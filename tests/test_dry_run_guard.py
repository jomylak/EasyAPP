"""Live check for the dry-run submit guard.

Needs a real browser: the guard is capture-phase DOM interception plus
fetch/XHR patching, and none of that is exercised by parsing the JS. Skips
itself when Chromium isn't installed so the normal suite stays runnable.
"""
import asyncio
import pathlib

import pytest

SERVER = pathlib.Path(__file__).parent.parent / "src/applypilot/apply/mcp_tools/server.py"
GUARD = SERVER.read_text().split('_DRY_RUN_GUARD_JS = """')[1].split('"""')[0]

PAGE = """
<form id="f" action="/apply" method="post">
  <input name="a"><button type="submit">Submit Application</button>
</form>
<button id="spa">Send my application</button>
<script>
window.__hits = [];
document.getElementById('spa').onclick = () => {
  fetch('/apply', {method: 'POST', body: 'x'})
    .then(() => window.__hits.push('spa-post-went-through')).catch(() => {});
};
document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault(); window.__hits.push('form-submit-handler-ran');
});
</script>
"""

# Workday-shaped: the password input for account creation is NOT nested
# inside the <form> that fires the submit event (a React portal renders it
# elsewhere in the DOM). isAuthForm must still recognize this as a
# login/signup screen -- checking only e.target's subtree missed it and
# blocked every real Create Account submit (2026-09-18 benchmark: two
# Workday jobs retried it 7-19 times each before giving up).
AUTH_PAGE = """
<input type="password" name="pw" style="display:none">
<form id="signup" action="/create-account" method="post">
  <input name="email"><button type="submit">Create Account</button>
</form>
<script>
window.__hits = [];
document.getElementById('signup').addEventListener('submit', e => {
  e.preventDefault(); window.__hits.push('create-account-handler-ran');
});
</script>
"""


# Oracle HCM OTP-screen-shaped: clicking "Send code" (not a final-submit
# label) runs the page's own handler, which advances the wizard via
# form.requestSubmit() with no button argument -- so the resulting submit
# event has submitter === null, same as any other framework-internal
# advance. No password field either, so isAuthForm doesn't cover it. The
# only signal this is safe is the real click that triggered it having landed
# on something that doesn't read as a final submit (2026-09-18: this exact
# shape got blocked and stalled a Mayo Clinic Oracle HCM dry run at the
# email-verification step).
OTP_PAGE = """
<form id="otpform" action="/send-code" method="post">
  <input name="email"><button id="send" type="button">Send code</button>
</form>
<script>
window.__hits = [];
document.getElementById('otpform').addEventListener('submit', e => {
  e.preventDefault(); window.__hits.push('otp-submit-handler-ran');
});
document.getElementById('send').addEventListener('click', () => {
  document.getElementById('otpform').requestSubmit();
});
</script>
"""


async def _run() -> list[str]:
    from playwright.async_api import async_playwright

    fails: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()

        async def _serve(route):
            await route.fulfill(status=200, content_type="text/html", body=PAGE)

        await page.route("**/*", _serve)
        await page.goto("http://example.test/form")
        await page.evaluate(GUARD)

        # A recognised submit button must not reach the page's own handler.
        await page.click("button[type=submit]")
        blocked = await page.evaluate("() => window.__applypilot_dry_run_blocked__")
        if not blocked or blocked.get("type") != "click":
            fails.append(f"submit click not blocked: {blocked}")
        if "form-submit-handler-ran" in await page.evaluate("() => window.__hits"):
            fails.append("page submit handler ran despite capture-phase block")

        # The case the click/submit handlers miss: a React button that posts
        # with fetch() and never submits a form.
        await page.evaluate("() => { window.__applypilot_dry_run_blocked__ = null; }")
        await page.click("#spa")
        await page.wait_for_timeout(300)
        blocked = await page.evaluate("() => window.__applypilot_dry_run_blocked__")
        if not blocked or blocked.get("type") != "fetch":
            fails.append(f"SPA fetch POST not blocked: {blocked}")
        if "spa-post-went-through" in await page.evaluate("() => window.__hits"):
            fails.append("fetch POST resolved -- the application would have been sent")

        # Reads have to keep working or we break the form we are filling.
        ok = await page.evaluate(
            "async () => { try { return (await fetch('/apply')).ok; }"
            " catch (e) { return 'threw:' + e.message; } }")
        if ok is not True:
            fails.append(f"same-origin GET was blocked, should not be: {ok}")

        await browser.close()
    return fails


async def _run_auth_form_case() -> list[str]:
    from playwright.async_api import async_playwright

    fails: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()

        async def _serve(route):
            await route.fulfill(status=200, content_type="text/html", body=AUTH_PAGE)

        await page.route("**/*", _serve)
        await page.goto("http://example.test/signup")
        await page.evaluate(GUARD)

        await page.click("button[type=submit]")
        blocked = await page.evaluate("() => window.__applypilot_dry_run_blocked__")
        if blocked:
            fails.append(f"Create Account wrongly blocked as a real submit: {blocked}")
        if "create-account-handler-ran" not in await page.evaluate("() => window.__hits"):
            fails.append("Create Account submit never reached the page's own handler")

        await browser.close()
    return fails


def test_dry_run_guard_blocks_submits_but_not_reads():
    pytest.importorskip("playwright")
    try:
        fails = asyncio.run(_run())
    except Exception as exc:  # no browser binary on this machine
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"chromium not installed: {exc}")
        raise
    assert not fails, "; ".join(fails)


def test_dry_run_guard_lets_through_auth_form_with_portaled_password_field():
    pytest.importorskip("playwright")
    try:
        fails = asyncio.run(_run_auth_form_case())
    except Exception as exc:  # no browser binary on this machine
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"chromium not installed: {exc}")
        raise
    assert not fails, "; ".join(fails)


async def _run_otp_case() -> list[str]:
    from playwright.async_api import async_playwright

    fails: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page()

        async def _serve(route):
            await route.fulfill(status=200, content_type="text/html", body=OTP_PAGE)

        await page.route("**/*", _serve)
        await page.goto("http://example.test/otp")
        await page.evaluate(GUARD)

        # Real click on a non-final button -> should be exempted.
        await page.click("#send")
        blocked = await page.evaluate("() => window.__applypilot_dry_run_blocked__")
        if blocked:
            fails.append(f"submitter-less advance after a real non-final click wrongly blocked: {blocked}")
        if "otp-submit-handler-ran" not in await page.evaluate("() => window.__hits"):
            fails.append("OTP submit never reached the page's own handler")

        # Same submitter-less path, but with NO preceding trusted click
        # (simulating browser_run_code_unsafe's dispatchEvent/el.click(),
        # which is isTrusted=false) -- must still be blocked.
        await page.evaluate("() => { window.__applypilot_dry_run_blocked__ = null; window.__hits = []; }")
        await page.evaluate("() => document.getElementById('otpform').requestSubmit()")
        blocked = await page.evaluate("() => window.__applypilot_dry_run_blocked__")
        if not blocked or blocked.get("type") != "form-submit":
            fails.append(f"scripted (untrusted) submitter-less submit was NOT blocked: {blocked}")

        await browser.close()
    return fails


def test_dry_run_guard_lets_through_submitterless_advance_after_real_click():
    pytest.importorskip("playwright")
    try:
        fails = asyncio.run(_run_otp_case())
    except Exception as exc:  # no browser binary on this machine
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"chromium not installed: {exc}")
        raise
    assert not fails, "; ".join(fails)
