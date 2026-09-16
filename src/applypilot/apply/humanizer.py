"""Ambient mouse movement for a worker's Chrome while a job is in progress.

The agent's own tool calls (browser_fill_form, browser_click, ...) execute
instantly with zero motion between them -- between actions the page is
completely static, which a real human's session never is. This attaches a
second CDP connection to the same running Chrome (same proven pattern as
screencast.py's live view: multiple CDP clients can share one DevTools
target without interfering) and moves the real OS-level cursor around the
viewport on an irregular schedule while the agent works.

Deliberately narrow scope, to avoid corrupting a real application:
  - Mouse movement only. No scrolling (would shift the page under element
    references the agent already snapshotted mid-fill) and no new tabs (the
    prompt has real logic that treats an unexpected new tab as something to
    investigate, e.g. an OAuth popup -- a stray tab from this module could
    get misread as one and derail the agent's own flow).
  - Never clicks, never touches page content -- pure cursor position, using
    Playwright's multi-step mouse.move so the path is a real series of
    mousemove events, not a single teleport.
  - Runs as an independent background task, not a tool the agent calls, so
    it costs zero LLM tokens/turns.
"""

import asyncio
import logging
import random
import threading

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_VIEWPORT_W, _VIEWPORT_H = 1920, 1080
_MIN_INTERVAL, _MAX_INTERVAL = 6.0, 20.0


def _human_interval() -> float:
    """Log-normal, not flat -- real idle gaps cluster short with an
    occasional long tail, and a uniform spread is itself a mild tell to
    anything fingerprinting action timing."""
    return min(_MAX_INTERVAL, max(_MIN_INTERVAL, random.lognormvariate(2.1, 0.45)))


def _bezier(p0: tuple[float, float], p1: tuple[float, float],
           p2: tuple[float, float], t: float) -> tuple[float, float]:
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return x, y


async def _move_mouse_humanlike(page, x0: float, y0: float, x1: float, y1: float) -> None:
    """Curved path (one random bulge point off the straight line) plus an
    occasional overshoot-and-correct near the target -- both real Fitts's-law
    artifacts that a straight `mouse.move(steps=N)` doesn't have, since that
    interpolates linearly between exactly two points."""
    dx, dy = x1 - x0, y1 - y0
    dist = max(1.0, (dx ** 2 + dy ** 2) ** 0.5)
    perp = (-dy / dist, dx / dist)
    bulge = random.uniform(-0.25, 0.25) * dist
    ctrl = ((x0 + x1) / 2 + perp[0] * bulge, (y0 + y1) / 2 + perp[1] * bulge)

    overshoot = random.random() < 0.35
    target = (x1, y1)
    if overshoot:
        target = (x1 + dx / dist * random.uniform(8, 20),
                  y1 + dy / dist * random.uniform(8, 20))

    steps = random.randint(12, 28)
    for i in range(1, steps + 1):
        px, py = _bezier((x0, y0), ctrl, target, i / steps)
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.004, 0.014))

    if overshoot:
        for i in range(1, 6):
            t = i / 5
            await page.mouse.move(target[0] + (x1 - target[0]) * t,
                                  target[1] + (y1 - target[1]) * t)
            await asyncio.sleep(random.uniform(0.006, 0.016))


async def _humanize_loop(port: int, stop_event: threading.Event) -> None:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            x, y = _VIEWPORT_W / 2, _VIEWPORT_H / 2
            while not stop_event.is_set():
                await asyncio.sleep(_human_interval())
                if stop_event.is_set() or not browser.is_connected():
                    break
                try:
                    context = browser.contexts[0] if browser.contexts else None
                    page = context.pages[-1] if context and context.pages else None
                    if page is None:
                        continue
                    nx = random.uniform(50, _VIEWPORT_W - 50)
                    ny = random.uniform(50, _VIEWPORT_H - 50)
                    await _move_mouse_humanlike(page, x, y, nx, ny)
                    x, y = nx, ny
                except Exception:
                    # The agent may have navigated, closed a tab, or the page
                    # may be mid-transition -- a missed movement is harmless,
                    # unlike a crashed background task.
                    logger.debug("Humanizer move skipped this cycle", exc_info=True)
    except Exception:
        logger.debug("Humanizer loop ended", exc_info=True)


def start(port: int) -> threading.Event:
    """Start the background humanizer for a worker's Chrome. Returns a stop
    Event -- set it (stop_humanizing) when the job/worker finishes."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=lambda: asyncio.run(_humanize_loop(port, stop_event)),
        daemon=True,
    )
    thread.start()
    return stop_event


def stop(stop_event: threading.Event | None) -> None:
    """Stop a humanizer started by start(). Safe to call with None."""
    if stop_event is not None:
        stop_event.set()
