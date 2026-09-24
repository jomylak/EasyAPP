"""Custom MCP tool server -- deterministic fixes for generic browser friction.

Registered as a third goose/Claude extension (``applytools``) alongside
``playwright`` and ``gmail``, pointed at the same ``--cdp-endpoint`` so it
drives the exact live tab the agent is already working in, not a separate
browser.

Scope rule: only tools for friction that recurs across almost every ATS
because it comes from *our own* tooling (Playwright + browser sandboxing),
never for ATS-specific business logic -- that stays as text in
``config/known_quirks/*.md``, added incrementally per platform. The first two
tools (upload/combobox) were picked from a frequency grep over real
``goose_*.txt`` transcripts. ``read_form_state`` came from a later pass
mining actual ``browser_evaluate``/``browser_run_code_unsafe`` call
arguments out of goose's local session DB
(``~/.local/share/goose/sessions/sessions.db``) -- narrated transcripts only
capture what the agent says, not the raw JS it writes, so that pass surfaced
a batch field-state-dump pattern (and the iCIMS iframe-reading case) that
transcript grep alone never would have.

Most tools fail loudly with an ``error: ...`` string rather than silently
no-opping when the page doesn't match the expected shape, so the agent falls
back to raw ``browser_*`` tools instead of getting stuck on a false success.
``decline_eeo`` is the one exception -- it's inherently best-effort (a page
may legitimately have no EEO section), so "nothing matched" is a valid,
informative result rather than a disguised failure.

``handle_captcha`` is a different kind of addition to this file: not friction
from our own tooling, but ~15,000 characters of raw JavaScript that used to
live directly in the prompt (detect + NoneCap solve/inject + CapSolver's
3-step createTask/poll/inject, repeated per vendor type) -- the single
largest chunk of the whole prompt, resent on every job regardless of whether
that job's ATS ever shows a captcha. Moving the (unchanged) vendor-dispatch
and budget logic into one deterministic tool cut prompt.py by about 18%.
Vendor strategy is the other captcha/proxy work's territory; this only moved
where the mechanics live, not what they do.
"""

import argparse
import asyncio
import json
import os
import random
import re
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import Page, async_playwright

mcp = MCPServer("applytools")

_cdp_endpoint: str | None = None
_playwright = None
_browser = None
_dry_run = False
_guarded_contexts: set[int] = set()

# Free network-usage telemetry: real transferred bytes per apply run, broken
# down by resource type/domain. Deliberately lives on THIS connection rather
# than a separate connect_over_cdp() observer process -- a standalone
# second CDP client (tried first) got TargetClosedError on every poll the
# moment a THIRD client (the test driver) disconnected, so it never saw a
# single byte. This module's _browser connection is already the proven
# one: the dry-run guard's frame.evaluate() calls read state reliably here
# across goose's whole run, so polling the same way for
# performance.getEntriesByType() rides the same proven path instead of
# opening a new connection with its own failure mode.
_NETSTATS_POLL_JS = """
() => {
  const nav = performance.getEntriesByType('navigation');
  const res = performance.getEntriesByType('resource');
  const out = [...nav, ...res].map(e => ({
    name: e.name,
    type: e.initiatorType || 'navigation',
    transferSize: e.transferSize || 0,
  }));
  performance.clearResourceTimings();
  return out;
}
"""
_netstats = {
    "by_type": defaultdict(int),
    "by_domain": defaultdict(int),
    "requests": 0,
    "pages_seen": 0,
}
_netstats_task = None


def _netstats_path() -> Path:
    port = (_cdp_endpoint or "").rsplit(":", 1)[-1]
    return Path(f"/tmp/applypilot_netstats_{port}.json")


def _netstats_write() -> None:
    try:
        result = {
            "total_bytes": sum(_netstats["by_type"].values()),
            "requests": _netstats["requests"],
            "pages": _netstats["pages_seen"],
            "by_type": dict(_netstats["by_type"]),
            "by_domain": dict(
                sorted(_netstats["by_domain"].items(), key=lambda kv: -kv[1])[:15]
            ),
        }
        _netstats_path().write_text(json.dumps(result))
    except Exception:
        pass


async def _netstats_poll_loop() -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            pages = [p for ctx in _browser.contexts for p in ctx.pages]
            _netstats["pages_seen"] = max(_netstats["pages_seen"], len(pages))
            for page in pages:
                try:
                    entries = await page.evaluate(_NETSTATS_POLL_JS)
                except Exception:
                    continue  # page navigating/closed mid-poll, skip this cycle
                for e in entries or []:
                    domain = urlparse(e["name"]).netloc
                    _netstats["by_type"][e["type"]] += e["transferSize"]
                    _netstats["by_domain"][domain] += e["transferSize"]
                    _netstats["requests"] += 1
            _netstats_write()
        except Exception:
            pass

# Blocks the click/submit itself (capture-phase, before any handler the page
# registered can run) instead of just asking the model not to do it -- a
# prompt instruction is advice the model can ignore, this can't be. Confirmed
# necessary 2026-09-17: a dry-run benchmark had the model click through to a
# real "Thank you for applying" page on SpaceX's Greenhouse form despite the
# prompt explicitly saying not to (see dry-run-not-enforced memory). Covers
# three paths to a real submit: a click on a submit-like element, a native
# `submit` event (also fires from pressing Enter in a field), and a direct
# `form.submit()` call. Runs in every frame, not just the main one, since
# Greenhouse/Workday-style embeds put the actual form in an iframe.
_DRY_RUN_GUARD_JS = """
(() => {
  if (window.__applypilot_dry_run_guard__) return;
  window.__applypilot_dry_run_guard__ = true;
  window.__applypilot_dry_run_blocked__ = null;
  const FINAL_SUBMIT_TEXT =
    /^(submit|apply now|apply|send application|finish( and)? apply|submit application)$/;
  const isSubmitLike = (el) => {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const text = (el.innerText || el.value || '').trim().toLowerCase();
    if (tag === 'input' && type === 'submit') return true;
    if ((tag === 'button' || el.getAttribute('role') === 'button')
        && FINAL_SUBMIT_TEXT.test(text)) {
      return true;
    }
    return false;
  };
  // Last real (OS-level, CDP-dispatched) click, regardless of what it hit --
  // used below to let a framework's own submitter-less form.submit() through
  // when it was clearly triggered by the agent clicking something that
  // wasn't a final-submit button (e.g. Oracle HCM's "Send code" on the OTP
  // screen). A model-scripted dispatchEvent/el.click() via
  // browser_run_code_unsafe has isTrusted=false and never sets this, so it
  // can't be used to manufacture the exemption -- only a real click can.
  let lastTrustedClick = null;
  document.addEventListener('click', (e) => {
    if (e.isTrusted) {
      lastTrustedClick = {
        ts: Date.now(),
        text: ((e.target && (e.target.innerText || e.target.value)) || '').trim(),
      };
    }
    let el = e.target;
    for (let i = 0; el && i < 6; i++, el = el.parentElement) {
      if (isSubmitLike(el)) {
        e.preventDefault();
        e.stopImmediatePropagation();
        window.__applypilot_dry_run_blocked__ =
          {type: 'click', text: (el.innerText || el.value || '').trim()};
        return;
      }
    }
  }, true);
  // A password field means this is a login/create-account form, not the
  // real application -- account creation is a required, legitimate step on
  // ATS's that gate the whole form behind a signup wall (confirmed on
  // Workday: blocking that submit left the agent stuck at "Create Account"
  // with no way to even reach the application form, at which point it
  // started trying raw fetch() calls to route around what looked like a
  // broken button). The real application form never has a password field.
  // Page-scoped, not form-scoped: on Workday's Create Account screen the
  // password inputs sit outside the <form> node the submit event fires on
  // (React portal), so form.querySelector('input[type=password]') never
  // matched and every real account-creation submit got misclassified as
  // the final application submit. Confirmed 2026-09-18: two Workday dry-run
  // benchmark jobs each retried Create Account 7-19 times (regular click,
  // find_and_click, raw dispatchEvent, React native-setter hacks) after
  // every attempt came back blocked, burning 100+ tool calls apiece before
  // giving up. "is this a login/signup screen" is a fact about the page,
  // not about which DOM subtree a given form node happens to nest under.
  const isAuthForm = () => !!document.querySelector('input[type="password"]');
  document.addEventListener('submit', (e) => {
    if (isAuthForm()) return;
    // A real click's SubmitEvent carries which button triggered it -- if
    // that button's text doesn't read as a final submit (e.g. a multi-step
    // wizard's "Next"/"Continue"), let it through. Confirmed necessary on
    // Oracle HCM: blocking every intermediate step's submit stalled the
    // agent on page 1, same failure shape as the Workday login case above.
    if (e.submitter && !isSubmitLike(e.submitter)) return;
    // No submitter means a framework did this programmatically (React/
    // Knockout's own state-machine advancing the wizard, or a script's
    // synthetic dispatchEvent) -- normally the one path with no positive
    // signal it's safe, so it stays blocked. Exception: a real click landed
    // in the last 500ms on something that doesn't read as a final submit
    // (e.g. Oracle HCM's OTP-screen "Send code"/"Next" button, which posts
    // via an internal handler with no SubmitEvent.submitter at all). That's
    // a trusted, human-shaped action pointed at an intermediate step, not a
    // scripted attempt to force a submit through -- browser_run_code_unsafe's
    // dispatchEvent/el.click() has isTrusted=false and can't produce it.
    // Single-use: consumed immediately below, so one real click can only
    // ever wave through the one submit it actually triggered -- otherwise a
    // scripted submit arriving shortly after some earlier, unrelated real
    // click could ride along on it (own test caught this at a 2s window).
    // Confirmed necessary 2026-09-18: Oracle's OTP send-code POST has no
    // submitter and no password field, so neither existing exemption covers
    // it, and it was getting blocked exactly like a real final submit.
    if (lastTrustedClick && (Date.now() - lastTrustedClick.ts) < 500
        && !FINAL_SUBMIT_TEXT.test(lastTrustedClick.text.toLowerCase())) {
      lastTrustedClick = null;
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    window.__applypilot_dry_run_blocked__ =
      {type: 'form-submit', action: e.target && e.target.action};
  }, true);
  const origFormSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (isAuthForm()) return origFormSubmit.apply(this, arguments);
    window.__applypilot_dry_run_blocked__ =
      {type: 'form.submit()', action: this.action};
  };
  // The click/submit handlers above only catch a real form submission or a
  // button whose text we recognise. A React form posts with fetch() from its
  // own onClick and does neither, so it would sail straight through. Block
  // the write request itself: same-origin POST/PUT/PATCH is the application
  // being sent, and a GET (analytics, autocomplete, session ping) is not.
  const isWrite = (m) => /^(POST|PUT|PATCH)$/i.test(m || 'GET');
  const sameOrigin = (u) => {
    try { return new URL(u, location.href).origin === location.origin; }
    catch (e) { return false; }
  };
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = (typeof input === 'string') ? input : (input && input.url);
    const method = (init && init.method) || (input && input.method) || 'GET';
    if (isWrite(method) && sameOrigin(url)) {
      window.__applypilot_dry_run_blocked__ = {type: 'fetch', action: url};
      return Promise.reject(new Error('blocked by applypilot dry run'));
    }
    return origFetch.apply(this, arguments);
  };
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__applypilot_blocked__ = isWrite(method) && sameOrigin(url);
    if (this.__applypilot_blocked__) {
      window.__applypilot_dry_run_blocked__ = {type: 'xhr', action: url};
    }
    return origOpen.apply(this, arguments);
  };
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    if (this.__applypilot_blocked__) return;
    return origSend.apply(this, arguments);
  };
})();
"""


async def _install_dry_run_guard(page: Page) -> None:
    """Idempotently arm the submit-blocking guard on every frame of ``page``.

    Installs via ``add_init_script`` on the context too, so frames created or
    navigated *after* this call (a multi-step Workday flow, an SSO redirect)
    get it automatically -- ``add_init_script`` only covers documents loaded
    after it's registered, not the one already sitting in the frame, hence
    also evaluating directly into every current frame.
    """
    context = page.context
    if id(context) not in _guarded_contexts:
        _guarded_contexts.add(id(context))
        await context.add_init_script(_DRY_RUN_GUARD_JS)
    for frame in page.frames:
        try:
            await frame.evaluate(_DRY_RUN_GUARD_JS)
        except Exception:
            pass  # detached/cross-origin frame mid-navigation; init_script covers it on reload


async def _get_page() -> Page:
    """The most recently opened page on the CDP-connected browser.

    Not page index 0 -- goose/the ATS may have opened new tabs (SSO
    redirects, a "review application" popup), and the one it's actually
    looking at is whichever was opened or focused last.
    """
    global _playwright, _browser, _netstats_task
    # ToolError, not a bare raise: the SDK strips the message off any other
    # exception and hands the model "Error executing tool <name>" with no
    # reason (mcpserver/tools/base.py). 32 such blanks in one week of traces
    # were all this function failing on a dead browser, and neither the model
    # nor the logs could tell that apart from a real tool bug.
    try:
        if _playwright is None:
            _playwright = await async_playwright().start()
        if _browser is None or not _browser.is_connected():
            _browser = await _playwright.chromium.connect_over_cdp(_cdp_endpoint)
    except Exception as exc:
        raise ToolError(f"browser not reachable at {_cdp_endpoint}: {exc}") from exc
    if _netstats_task is None:
        _netstats_task = asyncio.create_task(_netstats_poll_loop())
    for context in reversed(_browser.contexts):
        pages = context.pages
        if pages:
            page = pages[-1]
            if _dry_run:
                await _install_dry_run_guard(page)
            return page
    raise ToolError("no open page found on the CDP-connected browser")


@mcp.tool()
async def upload_resume(pdf_path: str) -> str:
    """Upload a resume PDF, bypassing the native OS file picker entirely.

    Playwright's ``set_input_files`` talks to the ``<input type=file>``
    element directly over CDP -- no real file-chooser dialog ever opens, so
    there's no sandboxed-picker escape and no need to first copy the file
    into a directory the picker is allowed to browse. Works even when the
    input is hidden behind a styled "Upload resume" button, which is the
    common case.

    Args:
        pdf_path: Absolute path to the resume PDF on this machine.

    Returns a short ``ok: ...`` / ``error: ...`` status string.
    """
    page = await _get_page()
    inputs = page.locator("input[type=file]")
    count = await inputs.count()
    if count == 0:
        return "error: no <input type=file> found on the current page"
    try:
        await inputs.first.set_input_files(pdf_path, timeout=10_000)
    except Exception as exc:
        # A prior browser_click on the visible "Upload Resume" button (before
        # this tool was called) opens a real native file-chooser dialog --
        # Playwright then blocks set_input_files on the original input until
        # that dialog is resolved, which usually surfaces as a timeout here.
        # We hold a separate CDP connection from the `playwright` extension's
        # own client, so we can't reliably intercept or dismiss that dialog
        # from this process -- the clean recovery is the OTHER extension's
        # browser_file_upload, which already owns that pending chooser.
        return (
            f"error: set_input_files failed: {exc}. If a native file-chooser "
            "dialog is already open (e.g. from clicking the upload button "
            "first), use browser_file_upload instead -- it owns that dialog, "
            "this tool cannot reach it from here."
        )
    await page.wait_for_timeout(1500)
    return f"ok: uploaded {pdf_path}"


def _label_variants(label_or_selector: str) -> list[str]:
    """The label as given, plus a version with trailing required-field
    punctuation/whitespace stripped.

    Forms usually render the "*" on a required field's label either via a
    separate DOM node or a CSS ::after pseudo-element -- either way it often
    never makes it into the element's accessible name. An agent naturally
    quotes the label as it visually reads ("Email*"), which then fails to
    match a real accessible name of plain "Email". Measured against real
    traces: this was the single biggest cause of read_field/
    fill_searchable_combobox "could not locate" errors.
    """
    variants = [label_or_selector]
    stripped = label_or_selector.rstrip(" \t *✱†‡:").strip()
    if stripped and stripped != label_or_selector:
        variants.append(stripped)
    return variants


async def _locate_combobox(page: Page, label_or_selector: str, occurrence: int = 0):
    """Best-effort trigger locator: label, then placeholder, then visible
    text -- each tried against the label as given and with a trailing
    required-marker stripped (see `_label_variants`) -- then a raw CSS
    selector as the last resort.

    ``occurrence`` picks the Nth match (0-indexed, DOM order) instead of
    always the first -- needed when a label is ambiguous, e.g. a form with
    both a Phone-group "Country" combobox and a mailing-address "Country"
    combobox. Measured against real traces: without this, the model tried
    to disambiguate by passing a snapshot-shorthand string as
    ``label_or_selector`` instead (like ``'Phone >> combobox "Country"'``,
    copied straight out of browser_snapshot's pretty-printed accessibility
    tree) -- that's not a real CSS selector or accessible name, so it always
    failed with "could not locate a combobox trigger", burning several turns
    before falling back to manual browser_click/browser_find.
    """
    for text in _label_variants(label_or_selector):
        candidates = [
            page.get_by_label(text),
            page.get_by_placeholder(text),
            page.get_by_text(text, exact=False),
        ]
        for locator in candidates:
            try:
                if await locator.count() > occurrence:
                    return locator.nth(occurrence)
            except Exception:
                continue
    try:
        locator = page.locator(label_or_selector)
        if await locator.count() > occurrence:
            return locator.nth(occurrence)
    except Exception:
        pass
    return None


async def _visible_texts(page: Page, css: str, limit: int = 15) -> list[str]:
    """Distinct visible text of elements matching `css`, in DOM order.

    Used to turn a bare "could not find X" into "here is what IS there", so the
    agent's next attempt picks a real option/label instead of guessing. Measured
    2026-09-23: fill_searchable_combobox failed 45% of the time (243/542) with
    no hint of which options existed, and each miss cost a snapshot + retry.
    """
    try:
        texts = await page.evaluate(
            """([css, limit]) => {
                const out = [];
                for (const el of document.querySelectorAll(css)) {
                    const r = el.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    const t = (el.innerText || el.getAttribute('aria-label') || el.value || '')
                        .trim().replace(/\\s+/g, ' ').slice(0, 80);
                    if (t && !out.includes(t)) out.push(t);
                    if (out.length >= limit) break;
                }
                return out;
            }""", [css, limit])
    except Exception:
        return []
    return texts


_OPTION_CSS = "[role=option], [role=listbox] li, li[id*=option], [class*=option]:not([class*=options])"
_COMBOBOX_LABEL_CSS = "label, [role=combobox], [aria-haspopup=listbox]"
_CLICKABLE_CSS = "button, a[href], [role=button], input[type=submit], input[type=button], [role=tab], [role=link]"


async def _hint(page: Page, css: str, what: str) -> str:
    texts = await _visible_texts(page, css)
    return f" -- visible {what}: {texts}" if texts else f" -- no visible {what} found"


async def _options_hint(page: Page) -> str:
    """Options of the combobox that just failed, then close it. The failed fill
    typed `value` as a live filter, which usually leaves the list at "No
    options" -- clear that text first so the list shows what it really holds."""
    try:
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(400)
    except Exception:
        pass
    hint = await _hint(page, _OPTION_CSS, "options")
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return hint


async def _click_jittered(locator, timeout: int = 5000) -> None:
    """Click at a randomized point inside the element instead of dead-center.

    Real users don't click pixel-perfect centroids; a locator's default
    .click() always does. Falls back to a plain center click when the
    bounding box can't be read (zero-size/off-screen elements some ATS
    widgets use for the real underlying input).
    """
    position = None
    try:
        box = await locator.bounding_box(timeout=timeout)
        if box and box["width"] > 4 and box["height"] > 4:
            position = {
                "x": box["width"] * random.uniform(0.3, 0.7),
                "y": box["height"] * random.uniform(0.3, 0.7),
            }
    except Exception:
        pass
    await locator.click(timeout=timeout, position=position)


@mcp.tool()
async def human_pause() -> str:
    """Pause for a random ~2-6 seconds, simulating a final human review beat
    before submitting. Call this once, right before clicking the final
    Submit/Apply button -- not between every field, which would turn one
    cheap tool call into many and burn real turns/cost for no benefit.

    A programmatic fill-then-submit with zero pause between finishing the
    form and clicking Submit is an unnatural, inhumanly fast pattern; this is
    a genuine wall-clock delay (not a fake signal), costs one extra tool
    call for the whole application, and adds no LLM tokens beyond that.
    """
    # Log-normal, not flat -- see humanizer._human_interval for why a
    # uniform spread is itself a mild timing tell.
    await asyncio.sleep(min(8.0, max(2.0, random.lognormvariate(1.15, 0.35))))
    return "ok: paused"


@mcp.tool()
async def fill_searchable_combobox(label_or_selector: str, value: str, occurrence: int = 0) -> str:
    """Fill a searchable OR static combobox/listbox in one deterministic call.

    Opens the trigger, then looks for the matching option immediately --
    static/native listboxes (confirmed on Ashby's EEO dropdowns) reveal every
    option as soon as they're opened, with no filter input to type into.
    Typing into one of these does nothing productive and previously burned a
    full 5-second wait on every failure (measured: 6 of 12 real
    fill_searchable_combobox calls on one Ashby form failed this way, on
    Ethnicity/Gender fields specifically). Only if the option isn't there
    within a short beat does this fall back to typing ``value`` as a filter
    and waiting again -- the original behavior, for genuinely searchable/
    typeahead comboboxes.

    Args:
        label_or_selector: Visible label text, placeholder text, or a raw
            CSS selector for the combobox's clickable trigger element. Plain
            text only -- do NOT paste a snapshot-shorthand fragment like
            ``'Phone >> combobox "Country"'`` copied out of browser_snapshot,
            that is not a real selector and will always fail to locate. If
            the label is ambiguous (e.g. a page with both a phone-country and
            a mailing-address-country combobox both labeled "Country"), pass
            the plain label plus ``occurrence`` instead.
        value: Text to match against options (and to type, for the
            searchable-combobox fallback path).
        occurrence: 0-indexed match to use when this label matches more than
            one element on the page, in DOM order (0 = first/default). Check
            a snapshot to see which position you want before guessing.

    Returns a short ``ok: ...`` / ``error: ...`` status string.
    """
    page = await _get_page()
    trigger = await _locate_combobox(page, label_or_selector, occurrence)
    if trigger is None:
        return (f"error: could not locate a combobox trigger for {label_or_selector!r} "
                f"(occurrence={occurrence})" + await _hint(page, _COMBOBOX_LABEL_CSS, "labels"))
    option_text = await _select_combobox_option(page, trigger, value)
    if option_text is None:
        hint = await _options_hint(page)
        return (f"error: combobox fill failed for {label_or_selector!r} -> {value!r}{hint}. "
                "Retry with the closest option text exactly as listed.")
    return f"ok: selected {option_text!r} in combobox {label_or_selector!r}"


async def _select_combobox_option(page: Page, trigger, value: str) -> str | None:
    """Core of `fill_searchable_combobox`, minus the locate/error-message
    wrapping -- shared with `human_fill_form` so a batched combobox field
    goes through the exact same static-then-searchable fallback path as the
    standalone tool.

    Returns the selected option's text, or None on any failure.
    """
    try:
        await _click_jittered(trigger)
    except Exception:
        return None

    option = page.get_by_role("option", name=value, exact=False).first
    try:
        await option.wait_for(state="visible", timeout=1200)
    except Exception:
        try:
            await page.keyboard.type(value, delay=40)
            await page.wait_for_timeout(400)
            await option.wait_for(state="visible", timeout=5000)
        except Exception:
            return None

    try:
        option_text = (await option.text_content() or value).strip()
        await _click_jittered(option)
    except Exception:
        return None
    return option_text


def _keystroke_delay_s() -> float:
    """Per-character delay, gaussian around real fast-typist speed, not a
    flat value -- flat inter-keystroke timing is a known bot-typing tell."""
    return max(0.02, random.gauss(0.07, 0.03))


async def _human_type(page: Page, value: str) -> None:
    """Type value one character at a time with per-keystroke jitter.

    Each call dispatches real keydown/keypress/input/keyup events, unlike
    browser_fill_form's underlying locator.fill() (one JS value-set + a
    single input event, no keystrokes at all -- the least human-looking
    part of the whole apply flow, and out of this project's control since
    it lives in the upstream @playwright/mcp package). This is the
    alternative for the fields worth the realism.
    """
    for ch in value:
        await page.keyboard.type(ch, delay=0)
        await asyncio.sleep(_keystroke_delay_s())


_TRUTHY = {"true", "yes", "1", "checked", "on"}


@mcp.tool()
async def human_fill_form(fields: list[dict]) -> str:
    """Fill a whole page of mixed-type fields -- text boxes, dropdowns,
    radios, checkboxes, and comboboxes -- in ONE call, with a read-back of
    what actually landed for each. This is the fill-AND-verify replacement
    for filling one field per turn then read_field-ing it: both steps used
    to cost their own turn per field (2N turns for N fields); this is one.

    Text fields get real per-keystroke typing (jittered delay) and a
    jittered click position, the human-typing counterpart to
    browser_fill_form, which sets values instantly with no keystroke events
    at all. The only added cost over browser_fill_form is real wall-clock
    time (~70ms/char), not tool calls or tokens.

    Field type is auto-detected from the located element -- a native
    <select> gets select_option, a checkbox/radio gets checked/unchecked,
    anything matching a custom combobox trigger (role=combobox,
    aria-haspopup=listbox, or a <div>/<input> wrapping a listbox -- same
    detection `fill_searchable_combobox` relies on structurally) goes
    through that tool's open-and-pick logic, and everything else is typed
    as text. Override with an explicit "type" if auto-detection ever guesses
    wrong: "text", "select", "checkbox", "radio", or "combobox".

    Args:
        fields: list of {"label_or_selector": str, "value": str,
            "occurrence": int (optional, default 0),
            "type": str (optional, auto-detected if omitted)}.
            Same label/placeholder/text/CSS-selector resolution as
            fill_searchable_combobox. For checkbox/radio, value is
            "true"/"yes"/"1" to check, anything else to uncheck. For
            select/combobox, value is the option's visible text.

    Returns ``ok: filled N field(s): label=<read-back state>, ...`` (with
    any per-field errors appended) or ``error: ...`` if every field failed.
    """
    page = await _get_page()
    filled: list[str] = []
    errors: list[str] = []
    for f in fields:
        label = f.get("label_or_selector", "")
        value = f.get("value", "")
        occurrence = f.get("occurrence", 0)
        field_type = f.get("type", "")
        locator = await _locate_combobox(page, label, occurrence)
        if locator is None:
            errors.append(f"{label!r}: not found")
            continue
        try:
            if not field_type:
                tag = await locator.evaluate("el => el.tagName.toLowerCase()")
                input_type = await locator.evaluate("el => el.type || ''")
                role = await locator.evaluate("el => el.getAttribute('role') || ''")
                haspopup = await locator.evaluate("el => el.getAttribute('aria-haspopup') || ''")
                if tag == "select":
                    field_type = "select"
                elif input_type in ("checkbox", "radio"):
                    field_type = input_type
                elif role == "combobox" or haspopup == "listbox":
                    field_type = "combobox"
                else:
                    field_type = "text"

            if field_type == "select":
                await locator.select_option(label=value, timeout=3000)
            elif field_type in ("checkbox", "radio"):
                if value if isinstance(value, bool) else str(value).strip().lower() in _TRUTHY:
                    await locator.check(timeout=3000)
                else:
                    await locator.uncheck(timeout=3000)
            elif field_type == "combobox":
                option_text = await _select_combobox_option(page, locator, value)
                if option_text is None:
                    hint = await _options_hint(page)
                    errors.append(f"{label!r}: combobox fill failed for {value!r}{hint}")
                    continue
            else:
                await _click_jittered(locator)
                await locator.fill("", timeout=3000)  # clear any existing/autofilled value
                await _human_type(page, value)

            parts = await _read_field_parts(page, locator)
            filled.append(f"{label}={' '.join(parts)}")
        except Exception as exc:
            errors.append(f"{label!r}: {exc}")
            continue
    if not filled:
        return "error: no fields filled -- " + "; ".join(errors)
    out = f"ok: filled {len(filled)} field(s): {', '.join(filled)}"
    if errors:
        out += " | errors: " + "; ".join(errors)
    return out


async def _read_field_parts(page: Page, locator) -> list[str]:
    """Value/checked state + validation state for one located field, as the
    short ``value="..." invalid=false`` fragments `read_field` and the
    batched `human_fill_form` read-back both return. Factored out so a
    caller filling N fields doesn't need a second read_field-shaped call per
    field to get the same information -- see `human_fill_form`.
    """
    tag = await locator.evaluate("el => el.tagName.toLowerCase()")
    input_type = await locator.evaluate("el => el.type || ''")
    parts = []
    if tag in ("input", "textarea", "select") and input_type not in ("checkbox", "radio"):
        value = await locator.input_value(timeout=3000)
        parts.append(f'value="{value}"')
    elif input_type in ("checkbox", "radio"):
        checked = await locator.is_checked(timeout=3000)
        parts.append(f"checked={str(checked).lower()}")
    else:
        text = (await locator.text_content() or "").strip()
        parts.append(f'text="{text[:200]}"')
    invalid = await locator.evaluate(
        "el => el.getAttribute('aria-invalid') === 'true' || el.classList.contains('error')"
    )
    parts.append(f"invalid={str(invalid).lower()}")
    if invalid:
        # aria-describedby is the standard way a form points a field at
        # its own error message -- cheaper than scanning the whole page
        # for red text, and it's what check_for_errors below also uses.
        described_by = await locator.evaluate("el => el.getAttribute('aria-describedby') || ''")
        if described_by:
            for id_ in described_by.split():
                try:
                    # An attribute selector, not a `#id` CSS token: real
                    # ids from these forms are often UUIDs (leading
                    # digit, colons) that `#id` rejects outright with a
                    # SyntaxError, taking down the whole read over what
                    # should just be a missing error message.
                    err_el = page.locator(f'[id="{id_}"]')
                    if await err_el.count() > 0:
                        err_text = (await err_el.first.text_content() or "").strip()
                        if err_text:
                            parts.append(f'error_text="{err_text[:200]}"')
                except Exception:
                    continue
    return parts


@mcp.tool()
async def read_field(label_or_selector: str, occurrence: int = 0) -> str:
    """Read one field's current value/state without a full-page snapshot.

    A `browser_snapshot` after every fill/click to confirm it landed dumps
    the whole page's accessibility tree -- tens of thousands of tokens, none
    of it cacheable, just to check one field. This targets the one element
    instead: same label/placeholder/selector resolution as
    `fill_searchable_combobox`, returning only its value/checked state and
    any validation-error text sitting next to it.

    Args:
        label_or_selector: Visible label text, placeholder text, or a raw
            CSS selector for the field to inspect. Plain text only -- see
            `fill_searchable_combobox` for why a copied snapshot-shorthand
            fragment will not resolve.
        occurrence: 0-indexed match to use when this label matches more than
            one element on the page, in DOM order (0 = first/default).

    Returns a short ``ok: <value>`` / ``error: ...`` status string, e.g.
    ``ok: value="Jane Doe" invalid=false`` or
    ``ok: checked=true`` for a checkbox/radio.
    """
    page = await _get_page()
    locator = await _locate_combobox(page, label_or_selector, occurrence)
    if locator is None:
        return f"error: could not locate a field for {label_or_selector!r} (occurrence={occurrence})"
    try:
        parts = await _read_field_parts(page, locator)
    except Exception as exc:
        return f"error: read failed for {label_or_selector!r}: {exc}"
    return "ok: " + " ".join(parts)


@mcp.tool()
async def check_for_errors() -> str:
    """Scan the current page for validation errors without a full snapshot.

    Looks only at the standard places a form puts an error: elements with
    role="alert", aria-invalid="true", or a class containing "error"/
    "invalid" that also sits inside a field wrapper (so permanent helper
    text like "File exceeds the maximum upload size of 100MB" or a country
    dial-code hint, which also carry "error"/"invalid" classes for styling
    but aren't validation state, don't get reported as errors). Returns just
    the real matches (name + message), which is normally a handful of short
    lines, instead of a full `browser_snapshot` whose whole purpose was to
    spot exactly this after a failed submit.

    Returns ``ok: no errors found`` or a newline-separated list of
    ``field: message`` (or ``message`` alone when no associated field name
    is found), capped at 15 entries.
    """
    page = await _get_page()
    if _dry_run:
        for frame in page.frames:
            try:
                blocked = await frame.evaluate("() => window.__applypilot_dry_run_blocked__")
            except Exception:
                continue
            if blocked:
                return (
                    f"error: DRY RUN -- a {blocked.get('type')} on "
                    f"{blocked.get('text') or blocked.get('action') or '(submit)'} was blocked. "
                    "This run must not submit. Stop trying to click Submit -- output "
                    "RESULT:APPLIED with a note that this was a dry run."
                )
    try:
        found = await page.evaluate("""
            () => {
                const seen = new Set();
                const out = [];
                function isVisible(el) {
                    if (el.offsetParent === null) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }
                const nodes = document.querySelectorAll(
                    '[role="alert"], [aria-invalid="true"], [class*="error" i], [class*="invalid" i]'
                );
                for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const isClassOnly = el.getAttribute('role') !== 'alert'
                        && el.getAttribute('aria-invalid') !== 'true';
                    if (isClassOnly && !el.closest(
                        'label, [class*="field" i], [class*="form-group" i], [class*="form-row" i]'
                    )) continue;
                    const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (!text || text.length > 300 || seen.has(text)) continue;
                    seen.add(text);
                    let fieldName = '';
                    const describedBy = el.getAttribute('aria-describedby');
                    if (el.getAttribute('aria-invalid') === 'true') {
                        fieldName = el.getAttribute('aria-label')
                            || el.getAttribute('name') || el.id || '';
                    }
                    out.push(fieldName ? `${fieldName}: ${text}` : text);
                    if (out.length >= 15) break;
                }
                return out;
            }
        """)
    except Exception as exc:
        return f"error: scan failed: {exc}"
    if not found:
        return "ok: no errors found"
    return "\n".join(found)


@mcp.tool()
async def read_form_state() -> str:
    """Dump every visible form field's current value/checked state in one call.

    Before submitting (or re-checking after a batch of fills), agents kept
    re-deriving the same JS -- `querySelectorAll('input, select, textarea')`
    plus a loop building a `{label: value}` summary -- instead of paying for
    a full `browser_snapshot` just to eyeball whether everything landed.
    Found this pattern well over 40 times across one batch of real runs, most
    often right before a submit click. This is that loop as a single
    deterministic tool, returning a compact list instead of the whole
    accessibility tree.

    Also walks same-origin iframes, not just the top document -- some ATS
    platforms (iCIMS is the confirmed case) render their entire application
    form inside a content iframe, which is otherwise reached by ad hoc
    `iframe.contentDocument` JS re-derived per run. Cross-origin iframes
    (e.g. a payment or SSO widget) are silently skipped, not errored on.

    Returns a newline-separated list of ``tag[type] "label" = "value"`` (or
    ``checked=true/false`` for checkboxes/radios), each line prefixed with
    ``[iframe]`` when it came from inside one. Hidden/zero-size fields are
    skipped. Capped at 80 fields with a truncation note if there are more.
    """
    page = await _get_page()
    js = """
        () => {
            function labelFor(el) {
                if (el.labels && el.labels.length) return el.labels[0].textContent.trim();
                return el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '';
            }
            function describe(doc) {
                const out = [];
                const seen = new Set();
                doc.querySelectorAll('input, select, textarea').forEach(el => {
                    const type = (el.type || '').toLowerCase();
                    if (['hidden', 'submit', 'button', 'file', 'image'].includes(type)) return;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return;
                    const label = labelFor(el).replace(/\\s+/g, ' ').trim().slice(0, 60);
                    let desc;
                    if (type === 'checkbox' || type === 'radio') {
                        desc = `${type} "${label}" name=${el.name || ''} checked=${el.checked}`;
                    } else if (el.tagName.toLowerCase() === 'select') {
                        const opt = el.options[el.selectedIndex];
                        desc = `select "${label}" = "${(opt ? opt.text : '').slice(0, 60)}"`;
                    } else {
                        desc = `${el.tagName.toLowerCase()} "${label}" = "${(el.value || '').slice(0, 80)}"`;
                    }
                    if (!seen.has(desc)) { seen.add(desc); out.push(desc); }
                });
                return out;
            }
            let fields = describe(document);
            for (const frame of document.querySelectorAll('iframe')) {
                try {
                    const doc = frame.contentDocument;
                    if (doc) fields = fields.concat(describe(doc).map(l => '[iframe] ' + l));
                } catch (e) {
                    // Cross-origin -- not reachable from here, skip silently.
                }
            }
            return fields;
        }
    """
    try:
        fields = await page.evaluate(js)
    except Exception as exc:
        return f"error: read_form_state failed: {exc}"
    if not fields:
        return "ok: no visible form fields found"
    max_fields = 80
    truncated = len(fields) > max_fields
    out = "\n".join(fields[:max_fields])
    if truncated:
        out += f"\n... ({len(fields)} fields total, truncated to {max_fields})"
    return "ok:\n" + out


@mcp.tool()
async def find_and_click(text_or_selector: str) -> str:
    """Locate an element by label/placeholder/visible text and click it, in one call.

    Merges the browser_find -> browser_click two-step -- the single most
    common consecutive tool-call pair across real runs (found back-to-back in
    19/27 traces of one batch) -- into one round trip. Uses the same
    label/placeholder/text/CSS-selector resolution as
    fill_searchable_combobox and read_field, so it recognizes the same things
    those do.

    Args:
        text_or_selector: Visible label text, placeholder text, visible text
            content, or a raw CSS selector for the element to click.

    Returns a short ``ok: ...`` / ``error: ...`` status string.
    """
    page = await _get_page()
    locator = await _locate_combobox(page, text_or_selector)
    if locator is None:
        return (f"error: could not locate a clickable element for {text_or_selector!r}"
                + await _hint(page, _CLICKABLE_CSS, "buttons/links"))
    try:
        await _click_jittered(locator)
    except Exception as exc:
        return f"error: click failed for {text_or_selector!r}: {exc}"
    return f"ok: clicked {text_or_selector!r}"


_DECLINE_PATTERN = re.compile(
    r"decline|prefer not|do not wish|don.t wish|not disclose|not to answer|choose not to",
    re.IGNORECASE,
)


@mcp.tool()
async def decline_eeo() -> str:
    """Select "decline/prefer not to answer" on every EEO-style field on the page.

    Scans native radio groups, checkboxes, and <select> elements for an
    option whose visible text matches a decline pattern ("decline", "prefer
    not to answer", "do not wish to provide this information", etc. --
    exactly the wording seen on real EEO/demographics sections) and selects
    it. Uses Playwright's own check()/select_option() (real events), not raw
    JS value-assignment, since several ATS platforms' custom widgets ignore
    JS-set values entirely (see the FIELD THAT RESISTS guidance).

    Scope note: only the top document -- an EEO section rendered inside a
    cross-origin or content iframe is not reached by this tool; fall back to
    manual interaction (or applytools__read_form_state to see what's there
    first) in that case.

    This is best-effort: a page may legitimately have no EEO section, or may
    use a custom (non-native) widget this doesn't recognize (e.g. a jQuery
    plugin with internal option IDs) -- "nothing found" is a valid result,
    not a failure. Always applytools__read_form_state afterward to confirm
    what actually changed before relying on it.

    Returns ``ok: no EEO/decline-style fields found (nothing changed)`` or
    ``ok: declined N field(s): ...`` listing what was selected.
    """
    page = await _get_page()
    changed: list[str] = []
    try:
        radios = page.locator("input[type=radio], input[type=checkbox]")
        for i in range(await radios.count()):
            el = radios.nth(i)
            try:
                if await el.is_checked():
                    continue
                text = await el.evaluate(
                    "el => (el.labels && el.labels[0] && el.labels[0].textContent) "
                    "|| (el.closest('label') && el.closest('label').textContent) "
                    "|| el.getAttribute('aria-label') || ''"
                )
            except Exception:
                continue
            if text and _DECLINE_PATTERN.search(text):
                try:
                    await el.check(timeout=3000)
                except Exception:
                    try:
                        await _click_jittered(el, timeout=3000)
                    except Exception:
                        continue
                changed.append(text.strip().replace("\n", " ")[:60])

        selects = page.locator("select")
        for i in range(await selects.count()):
            sel = selects.nth(i)
            try:
                options = await sel.evaluate("el => Array.from(el.options).map(o => o.text)")
            except Exception:
                continue
            match = next((o for o in options if _DECLINE_PATTERN.search(o)), None)
            if match:
                try:
                    await sel.select_option(label=match, timeout=3000)
                except Exception:
                    continue
                changed.append(match.strip()[:60])
    except Exception as exc:
        return f"error: decline_eeo failed: {exc}"
    if not changed:
        return "ok: no EEO/decline-style fields found (nothing changed)"
    return f"ok: declined {len(changed)} field(s): " + "; ".join(changed[:10])


_last_page_summary: list[str] = []

_PAGE_SUMMARY_JS = """
    () => {
        function labelFor(el) {
            if (el.labels && el.labels.length) return el.labels[0].textContent.trim();
            return el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '';
        }
        const out = [];
        out.push('URL: ' + location.href);
        out.push('TITLE: ' + document.title);
        document.querySelectorAll('h1, h2, h3').forEach(h => {
            const t = h.textContent.trim().replace(/\\s+/g, ' ');
            if (t) out.push('HEADING: ' + t.slice(0, 100));
        });
        document.querySelectorAll('[role="alert"], .error, .validation-error, .field-error').forEach(el => {
            const t = el.textContent.trim().replace(/\\s+/g, ' ');
            if (t) out.push('ALERT: ' + t.slice(0, 150));
        });
        document.querySelectorAll('button, a[role="button"], [role="button"]').forEach(el => {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;
            const t = (el.textContent || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
            if (t) out.push('BUTTON: ' + t.slice(0, 80));
        });
        document.querySelectorAll('input, select, textarea').forEach(el => {
            const type = (el.type || '').toLowerCase();
            if (['hidden', 'submit', 'button', 'file', 'image'].includes(type)) return;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;
            const label = labelFor(el).replace(/\\s+/g, ' ').trim().slice(0, 60);
            if (type === 'checkbox' || type === 'radio') {
                out.push(`FIELD: ${type} "${label}" checked=${el.checked}`);
            } else if (el.tagName.toLowerCase() === 'select') {
                const opt = el.options[el.selectedIndex];
                out.push(`FIELD: select "${label}" = "${(opt ? opt.text : '').slice(0, 60)}"`);
            } else {
                out.push(`FIELD: ${el.tagName.toLowerCase()} "${label}" = "${(el.value || '').slice(0, 60)}"`);
            }
        });
        return out;
    }
"""


@mcp.tool()
async def snapshot_diff() -> str:
    """Report what changed on the page since the last check -- not a full snapshot.

    UNVERIFIED against a live page as of this writing -- try this on a real
    application before relying on it, and fall back to browser_snapshot if
    the diff looks wrong or incomplete.

    The click -> browser_snapshot -> click -> browser_snapshot loop (found
    back-to-back in a third of real traces) re-dumps the ENTIRE page's
    accessibility tree just to confirm one action landed -- most of that
    tree is identical to the last snapshot and costs full-price tokens
    anyway, since it's new content on every call. This tool keeps a
    lightweight summary (headings, alerts, buttons, and form field
    states -- not full markup) from the last time it was called *in this
    same goose/Claude session* and returns only the lines that were added or
    removed since then.

    This is a verification tool, not a targeting one: it does NOT produce
    the ref= identifiers browser_click needs. Use it to confirm "did that
    click actually do something," not to find what to click next -- for
    that, a real browser_snapshot is still required (it has its own,
    separate element-reference system this tool cannot see into).

    First call on a given session has nothing to diff against, so it returns
    the full current summary instead (still much smaller than a real
    snapshot) and treats that as the new baseline.

    Returns ``ok: no changes detected since last check``, or ``ADDED:``/
    ``REMOVED:`` sections listing what changed, each capped at 40 lines.
    """
    page = await _get_page()
    try:
        current = await page.evaluate(_PAGE_SUMMARY_JS)
    except Exception as exc:
        return f"error: snapshot_diff failed: {exc}"

    global _last_page_summary
    prev = _last_page_summary
    if not prev:
        _last_page_summary = current
        return "ok: first check this session, no prior state to diff -- current page:\n" + "\n".join(current[:80])

    prev_set, curr_set = set(prev), set(current)
    added = [line for line in current if line not in prev_set]
    removed = [line for line in prev if line not in curr_set]
    _last_page_summary = current

    if not added and not removed:
        return "ok: no changes detected since last check"
    parts = []
    if added:
        parts.append("ADDED:\n" + "\n".join(added[:40]))
    if removed:
        parts.append("REMOVED:\n" + "\n".join(removed[:40]))
    return "ok:\n" + "\n\n".join(parts)


_CAPTCHA_DETECT_JS = """
    () => {
        const r = {};
        const url = window.location.href;
        const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
        if (hc) { r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey; }
        if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {
            const el = document.querySelector('[data-sitekey]');
            if (el) { r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }
        }
        if (!r.type) {
            const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
            if (cf) {
                r.type = 'turnstile';
                r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
                if (cf.dataset.action) r.action = cf.dataset.action;
                if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
            }
        }
        if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {
            r.type = 'turnstile_script_only';
        }
        if (!r.type) {
            const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
            if (s) {
                const m = s.src.match(/render=([^&]+)/);
                if (m && m[1] !== 'explicit') { r.type = 'recaptchav3'; r.sitekey = m[1]; }
            }
        }
        if (!r.type) {
            const rc = document.querySelector('.g-recaptcha');
            if (rc) { r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }
        }
        if (!r.type && document.querySelector('script[src*="recaptcha"]')) {
            const el = document.querySelector('[data-sitekey]');
            if (el) { r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }
        }
        if (!r.type) {
            const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
            if (fc) { r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }
        }
        if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {
            const el = document.querySelector('[data-pkey]');
            if (el) { r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }
        }
        if (r.type) { r.url = url; return r; }
        return null;
    }
"""

_HCAPTCHA_INJECT_JS = """
    (token) => {
        const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
        if (ta) ta.value = token;
        document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(
            f => f.setAttribute('data-hcaptcha-response', token));
        const cb = document.querySelector('[data-hcaptcha-widget-id]');
        if (cb && window.hcaptcha) {
            try { window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); } catch (e) {}
        }
        return 'injected';
    }
"""

_RECAPTCHA_INJECT_JS = """
    (token) => {
        document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {
            el.value = token; el.style.display = 'block';
        });
        if (window.___grecaptcha_cfg) {
            const clients = window.___grecaptcha_cfg.clients;
            for (const key in clients) {
                const walk = (obj, d) => {
                    if (d > 4 || !obj) return;
                    for (const k in obj) {
                        if (typeof obj[k] === 'function' && k.length < 3) {
                            try { obj[k](token); } catch (e) {}
                        } else if (typeof obj[k] === 'object') {
                            walk(obj[k], d + 1);
                        }
                    }
                };
                walk(clients[key], 0);
            }
        }
        return 'injected';
    }
"""

_TURNSTILE_INJECT_JS = """
    (token) => {
        const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
        if (inp) inp.value = token;
        if (window.turnstile) {
            try {
                const w = document.querySelector('.cf-turnstile');
                if (w) window.turnstile.getResponse(w);
            } catch (e) {}
        }
        return 'injected';
    }
"""

_FUNCAPTCHA_INJECT_JS = """
    (token) => {
        const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
        if (inp) inp.value = token;
        if (window.ArkoseEnforcement) {
            try { window.ArkoseEnforcement.setConfig({data: {blob: token}}); } catch (e) {}
        }
        return 'injected';
    }
"""

_CAPSOLVER_TASK_TYPES_PROXY = {
    "recaptchav2": "ReCaptchaV2Task",
    "recaptchav3": "ReCaptchaV3Task",
    "turnstile": "AntiTurnstileTask",
    "funcaptcha": "FunCaptchaTask",
}
_CAPSOLVER_TASK_TYPES_PROXYLESS = {
    "recaptchav2": "ReCaptchaV2TaskProxyLess",
    "recaptchav3": "ReCaptchaV3TaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
    "funcaptcha": "FunCaptchaTaskProxyLess",
}
_CAPSOLVER_INJECT_JS = {
    "recaptchav2": _RECAPTCHA_INJECT_JS,
    "recaptchav3": _RECAPTCHA_INJECT_JS,
    "turnstile": _TURNSTILE_INJECT_JS,
    "funcaptcha": _FUNCAPTCHA_INJECT_JS,
}

# Per-session budget state -- this process lives for one job, so these reset
# naturally between jobs. Enforced here (not just described in the prompt) so
# a wayward agent can't exceed the policy by simply calling the tool again.
_hcaptcha_attempted: set[str] = set()
_captcha_instance_attempts: dict[str, int] = {}
_capsolver_total_attempts = 0

_CAPSOLVER_MAX_PER_INSTANCE = 2
_CAPSOLVER_MAX_TOTAL = 3


def _http_post_json(url: str, payload: dict, timeout: float, extra_headers: dict) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **extra_headers}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


@mcp.tool()
async def handle_captcha(proxy_string: str = "") -> str:
    """Detect, solve, and inject a CAPTCHA on the current page -- one call.

    Replaces what used to be ~15,000 characters of raw JavaScript in the
    prompt (a DOM-detection function, NoneCap's solve+inject, and CapSolver's
    3-step createTask/poll/inject repeated per vendor type) with a single
    tool call. The vendor split is unchanged: hCaptcha -> NoneCap (one
    attempt only, hard stop on failure -- its image challenges are built to
    defeat exactly this kind of model, so don't retry or attempt it
    visually); reCAPTCHA v2/v3, Turnstile, FunCaptcha -> CapSolver (up to 2
    cycles per captcha instance, 3 total per job). This tool enforces both
    budgets itself via module-level state, not just prompt instructions, so
    it will refuse further attempts once exhausted rather than relying on the
    agent to self-police.

    Args:
        proxy_string: This job's CapSolver-format proxy
            ("type:host:port:user:pass"), if one is active for this job.
            Passed to CapSolver as-is (so it solves through the same egress
            IP the browser is using) and reformatted internally for NoneCap,
            which wants a plain URL instead. Pass empty string if there's no
            active proxy for this job.

    Returns:
        ``ok: no captcha detected`` -- nothing found, continue normally.
        ``ok: <type> solved and injected`` -- success; click Submit/Verify/
        Continue if the page didn't auto-advance on its own.
        ``error: ...`` -- see the message. hCaptcha errors are always a hard
        stop (output RESULT:FAILED:captcha, do not retry or solve visually).
        CapSolver errors after budget exhaustion mean go to the manual
        fallback (audio/accessibility button, or a simple text/logic puzzle)
        or output RESULT:CAPTCHA if nothing applies.
    """
    page = await _get_page()
    try:
        detection = await page.evaluate(_CAPTCHA_DETECT_JS)
    except Exception as exc:
        return f"error: detection failed: {exc}"

    if detection is not None and detection.get("type") == "turnstile_script_only":
        await page.wait_for_timeout(3000)
        try:
            detection = await page.evaluate(_CAPTCHA_DETECT_JS)
        except Exception as exc:
            return f"error: re-detection failed: {exc}"

    if detection is None:
        return "ok: no captcha detected"

    ctype = detection.get("type")
    sitekey = detection.get("sitekey")
    page_url = detection.get("url") or page.url
    if not sitekey:
        # No sitekey = nothing any solver can work with, and the manual
        # fallback never works on these either: 2026-09-23 traces show runs
        # that got this verdict kept going for a median 85 more tool calls
        # before giving up anyway.
        return (f"error: detected {ctype} but no sitekey found -- unsolvable. "
                "Output RESULT:CAPTCHA now; do not attempt a manual fallback")

    instance_key = f"{ctype}:{sitekey}"

    if ctype == "hcaptcha":
        if instance_key in _hcaptcha_attempted:
            return "error: hcaptcha already attempted once for this instance (hard stop, do not retry)"
        _hcaptcha_attempted.add(instance_key)

        nonecap_key = os.environ.get("NONECAP_API_KEY", "")
        if not nonecap_key:
            return "error: NONECAP_API_KEY not configured, cannot solve hCaptcha (hard stop)"

        nonecap_proxy = ""
        if proxy_string:
            try:
                ptype, phost, pport, puser, ppass = proxy_string.split(":", 4)
                nonecap_proxy = f"{ptype}://{puser}:{ppass}@{phost}:{pport}"
            except ValueError:
                pass

        payload = {"type": "hcaptcha", "sitekey": sitekey, "url": page_url}
        if nonecap_proxy:
            payload["proxy"] = nonecap_proxy

        try:
            result = await asyncio.to_thread(
                _http_post_json,
                "https://api.nonecap.com/v1/solves?wait=60",
                payload,
                65.0,
                {"Authorization": f"Bearer {nonecap_key}"},
            )
        except Exception as exc:
            return f"error: hcaptcha solve request failed: {exc} (hard stop, do not retry)"

        token = result.get("token")
        if result.get("status") != "solved" or not token:
            return f"error: hcaptcha solve failed: {result} (hard stop, do not retry or attempt visually)"

        try:
            await page.evaluate(_HCAPTCHA_INJECT_JS, token)
        except Exception as exc:
            return f"error: hcaptcha token injection failed: {exc}"
        await page.wait_for_timeout(2000)
        return "ok: hcaptcha solved and injected"

    if ctype not in _CAPSOLVER_TASK_TYPES_PROXY:
        return f"error: unrecognized captcha type {ctype!r}, try manual fallback"

    global _capsolver_total_attempts
    instance_attempts = _captcha_instance_attempts.get(instance_key, 0)
    if instance_attempts >= _CAPSOLVER_MAX_PER_INSTANCE or _capsolver_total_attempts >= _CAPSOLVER_MAX_TOTAL:
        return "error: capsolver budget exhausted for this run -- go to manual fallback or RESULT:CAPTCHA"

    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if not capsolver_key:
        return "error: CAPSOLVER_API_KEY not configured, cannot solve -- try manual fallback"

    _captcha_instance_attempts[instance_key] = instance_attempts + 1
    _capsolver_total_attempts += 1

    task_types = _CAPSOLVER_TASK_TYPES_PROXY if proxy_string else _CAPSOLVER_TASK_TYPES_PROXYLESS
    task: dict = {
        "type": task_types[ctype],
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if proxy_string:
        task["proxy"] = proxy_string
    if ctype == "recaptchav3":
        task["pageAction"] = detection.get("action") or "submit"
    if ctype == "turnstile":
        meta = {k: detection[k] for k in ("action", "cdata") if detection.get(k)}
        if meta:
            task["metadata"] = meta

    try:
        create_result = await asyncio.to_thread(
            _http_post_json,
            "https://api.capsolver.com/createTask",
            {"clientKey": capsolver_key, "task": task},
            15.0,
            {},
        )
    except Exception as exc:
        return f"error: capsolver createTask failed: {exc}"
    if create_result.get("errorId"):
        return f"error: capsolver createTask error: {create_result}"
    task_id = create_result.get("taskId")
    if not task_id:
        return f"error: capsolver createTask returned no taskId: {create_result}"

    token = None
    for _ in range(10):
        await page.wait_for_timeout(3000)
        try:
            poll_result = await asyncio.to_thread(
                _http_post_json,
                "https://api.capsolver.com/getTaskResult",
                {"clientKey": capsolver_key, "taskId": task_id},
                15.0,
                {},
            )
        except Exception as exc:
            return f"error: capsolver poll failed: {exc}"
        if poll_result.get("errorId"):
            return f"error: capsolver solve error: {poll_result}"
        if poll_result.get("status") == "ready":
            solution = poll_result.get("solution") or {}
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            break
    if not token:
        return "error: capsolver did not return a solution within 30s"

    try:
        await page.evaluate(_CAPSOLVER_INJECT_JS[ctype], token)
    except Exception as exc:
        return f"error: token injection failed: {exc}"
    await page.wait_for_timeout(2000)
    return f"ok: {ctype} solved and injected"


def main() -> None:
    global _cdp_endpoint, _dry_run
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-endpoint", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    _cdp_endpoint = args.cdp_endpoint
    _dry_run = args.dry_run
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
