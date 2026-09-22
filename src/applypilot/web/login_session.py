"""Interactive remote-login window: lets the user manually sign into a job
board (Jobright, etc.) inside the real persistent enrichment Chrome profile,
over the same loopback-only websocket connection the worker screencast
already uses -- no new port, no CDP exposed off the VM.

One shared session at a time (this is a manual, occasional action, not
something workers need concurrent access to). Closes itself after
_IDLE_TIMEOUT_SECONDS with no viewer connected, so a forgotten tab doesn't
keep a headed Chrome (and the enrichment profile lock) alive indefinitely --
enrichment's own launch_persistent_context call would otherwise fail while
this is running.
"""
import asyncio
import contextlib
import logging

from fastapi import WebSocket, WebSocketDisconnect
from playwright.async_api import async_playwright

from applypilot import config

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SECONDS = 600

_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1280,
    "maxHeight": 800,
    "everyNthFrame": 1,
}

_lock = asyncio.Lock()
_playwright = None
_context = None
_page = None
_idle_task: asyncio.Task | None = None


async def _close_locked() -> None:
    global _playwright, _context, _page, _idle_task
    if _idle_task:
        _idle_task.cancel()
        _idle_task = None
    if _context:
        with contextlib.suppress(Exception):
            await _context.close()
    if _playwright:
        with contextlib.suppress(Exception):
            await _playwright.stop()
    _context = _page = _playwright = None


async def _arm_idle_close() -> None:
    global _idle_task
    if _idle_task:
        _idle_task.cancel()

    async def _wait_then_close():
        await asyncio.sleep(_IDLE_TIMEOUT_SECONDS)
        async with _lock:
            logger.info("login_session: idle timeout, closing")
            await _close_locked()

    _idle_task = asyncio.ensure_future(_wait_then_close())


async def open_session(url: str) -> None:
    """Launch (or reuse) the persistent enrichment profile, navigated to `url`."""
    global _playwright, _context, _page
    async with _lock:
        if _context is None:
            config.ENRICHMENT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            _playwright = await async_playwright().start()
            _context = await _playwright.chromium.launch_persistent_context(
                str(config.ENRICHMENT_PROFILE_DIR), headless=False,
                # Matches _SCREENCAST_PARAMS' maxWidth/maxHeight exactly so a
                # screencast frame is always the real viewport 1:1 -- the
                # frontend maps click coordinates off the displayed image
                # size alone, with no separate scale-metadata handshake.
                viewport={"width": _SCREENCAST_PARAMS["maxWidth"], "height": _SCREENCAST_PARAMS["maxHeight"]},
            )
            _page = _context.pages[0] if _context.pages else await _context.new_page()
        await _page.goto(url)


async def stream(ws: WebSocket) -> None:
    """Bidirectional: screencast frames out, click/key input in.

    Reuses web/screencast.py's frame-relay shape, but the receive loop does
    real work here instead of just blocking for disconnect -- each inbound
    message is a `{type, ...}` input event forwarded to CDP's Input domain.
    """
    await ws.accept()
    async with _lock:
        if _context is None or _page is None:
            await ws.close(code=4004, reason="no session open")
            return
        if _idle_task:
            _idle_task.cancel()
        page, context = _page, _context

    try:
        cdp = await context.new_cdp_session(page)

        async def on_frame(frame: dict) -> None:
            try:
                await ws.send_text(frame["data"])
            except Exception:
                return
            with contextlib.suppress(Exception):
                await cdp.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})

        cdp.on("Page.screencastFrame", lambda f: asyncio.ensure_future(on_frame(f)))
        await cdp.send("Page.startScreencast", _SCREENCAST_PARAMS)

        while True:
            raw = await ws.receive_json()
            await _dispatch_input(cdp, raw)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("login_session: stream ended: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")
        async with _lock:
            await _arm_idle_close()


async def _dispatch_input(cdp, ev: dict) -> None:
    t = ev.get("type")
    try:
        if t in ("mousePressed", "mouseReleased", "mouseMoved"):
            await cdp.send("Input.dispatchMouseEvent", {
                "type": t, "x": ev["x"], "y": ev["y"],
                "button": ev.get("button", "left"), "clickCount": ev.get("clickCount", 1),
            })
        elif t == "wheel":
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": ev["x"], "y": ev["y"],
                "deltaX": ev.get("deltaX", 0), "deltaY": ev.get("deltaY", 0),
            })
        elif t in ("keyDown", "keyUp", "rawKeyDown"):
            await cdp.send("Input.dispatchKeyEvent", {
                "type": t, "key": ev.get("key", ""), "code": ev.get("code", ""),
                "text": ev.get("text", ""),
                "windowsVirtualKeyCode": ev.get("keyCode", 0),
                "nativeVirtualKeyCode": ev.get("keyCode", 0),
            })
        elif t == "char":
            await cdp.send("Input.insertText", {"text": ev.get("text", "")})
    except Exception as exc:
        logger.debug("login_session: input dispatch failed: %s", exc)


async def close_session() -> None:
    async with _lock:
        await _close_locked()
