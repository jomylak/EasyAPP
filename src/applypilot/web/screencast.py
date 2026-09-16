"""CDP screencast bridge: streams a worker's headful Chrome to a browser tab.

Opt-in and per-worker: a viewer connects only while a job's expansion panel
is open on the frontend, so a worker nobody is watching costs nothing extra.
Reuses the CDP port each worker's Chrome already exposes
(apply/chrome.py's BASE_CDP_PORT + worker_id) -- Chrome's DevTools protocol
happily serves multiple simultaneous client connections, so this rides
alongside the agent's own Playwright MCP session without interfering with
it or the job it's running.

Deliberately does not call Browser.close(): this Playwright connection did
not launch the browser, only attached to it, and closing the viewer's
websocket must never risk tearing down a Chrome instance mid-application.
"""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect
from playwright.async_api import async_playwright

from applypilot.apply.chrome import BASE_CDP_PORT

logger = logging.getLogger(__name__)

# Deliberately low: this is a live preview of form-filling, not a recording.
# Smaller frames and jpeg at modest quality keep the extra encode/network
# cost on the VM small even with a couple of panels open at once.
_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 50,
    "maxWidth": 960,
    "maxHeight": 640,
    "everyNthFrame": 1,
}


async def stream_worker(ws: WebSocket, worker_id: int) -> None:
    """Relay CDP screencast frames for `worker_id`'s Chrome to `ws`.

    Runs until the client disconnects or the worker's Chrome is unreachable.
    Each connection gets its own short-lived Playwright driver process --
    screencast viewing is occasional and brief, not something that needs a
    process kept warm for the life of the server.
    """
    await ws.accept()
    port = BASE_CDP_PORT + worker_id
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[-1] if browser.contexts else None
            page = context.pages[-1] if context and context.pages else None
            if page is None:
                await ws.close(code=4004, reason="no page")
                return

            cdp = await context.new_cdp_session(page)

            async def on_frame(frame: dict) -> None:
                try:
                    await ws.send_text(frame["data"])
                except Exception:
                    return
                try:
                    await cdp.send("Page.screencastFrameAck",
                                    {"sessionId": frame["sessionId"]})
                except Exception:
                    pass

            def _handle(frame: dict) -> None:
                asyncio.ensure_future(on_frame(frame))

            cdp.on("Page.screencastFrame", _handle)
            await cdp.send("Page.startScreencast", _SCREENCAST_PARAMS)

            try:
                # Frames flow one-way via the event handler above; this just
                # blocks until the viewer goes away.
                while True:
                    await ws.receive_text()
            finally:
                try:
                    await cdp.send("Page.stopScreencast")
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("[worker-%d] screencast unavailable: %s", worker_id, exc)
        try:
            await ws.close(code=4000, reason="unavailable")
        except Exception:
            pass
