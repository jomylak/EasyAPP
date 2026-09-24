"""Browser smoke + monkey tests for the web UI.

Boots `applypilot serve` against a throwaway APPLYPILOT_DIR seeded with fake
jobs, then drives it with headless Chromium. Any uncaught JS error, console
error or 5xx response fails the test -- those are the bugs a human would
otherwise only find by clicking around.

Anything that could start real work (launching apply runs, Gmail scans,
credential checks, login sessions) is aborted at the network layer, so the
monkey can click whatever it likes.
"""

import random
import re

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

BLOCKED = re.compile(r"/api/(launch|stop|stop-all|gmail-scan|credential-check|login-session)")


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed locally
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        pg.on("response", lambda r: r.status >= 500 and errors.append(f"{r.status} {r.url}"))
        pg.route(BLOCKED, lambda route: route.abort())
        pg.errors = errors
        pg.goto(server)
        pg.wait_for_load_state("networkidle")
        yield pg
        browser.close()


def _ignorable(err: str) -> bool:
    # Aborted requests to BLOCKED endpoints surface as console network errors.
    return "net::ERR_FAILED" in err or "Failed to fetch" in err


def _assert_clean(pg):
    real = [e for e in pg.errors if not _ignorable(e)]
    assert not real, "\n".join(real)


def test_every_tab_renders_without_errors(page):
    for name in ["Browse", "Dashboard", "Settings", "Browse"]:
        page.get_by_role("button", name=re.compile(rf"^{name}$", re.I)).first.click()
        page.wait_for_load_state("networkidle")
    page.get_by_text("Software Engineer Intern", exact=False).first.wait_for(timeout=10_000)
    _assert_clean(page)


@pytest.mark.parametrize("seed", [1, 2])
def test_monkey_clicking_never_errors(page, seed):
    """Random clicks/typing across the whole UI. The seed makes a failure replayable."""
    rng = random.Random(seed)
    for step in range(150):
        targets = page.locator("button:visible, input:visible, select:visible, tr:visible").all()
        if not targets:
            break
        el = rng.choice(targets)
        try:
            tag = el.evaluate("e => e.tagName + ':' + (e.type || '')")
            if tag.startswith("INPUT:") and tag not in ("INPUT:checkbox", "INPUT:radio"):
                el.fill(rng.choice(["", "0", "-1", "99999999", "zz'\"<>", "新"]), timeout=1000)
            elif tag.startswith("SELECT"):
                opts = el.locator("option").all_inner_texts()
                if opts:
                    el.select_option(label=rng.choice(opts), timeout=1000)
            else:
                el.click(timeout=1000, click_count=rng.choice([1, 1, 2]))
        except sync_api.Error:
            continue  # detached/covered element -- normal while the UI re-renders
        page.wait_for_timeout(50)
        real = [e for e in page.errors if not _ignorable(e)]
        assert not real, f"seed={seed} step={step} after {tag}:\n" + "\n".join(real)
