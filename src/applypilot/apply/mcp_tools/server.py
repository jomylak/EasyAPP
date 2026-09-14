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
"""

import argparse
import re

from mcp.server.mcpserver import MCPServer
from playwright.async_api import Page, async_playwright

mcp = MCPServer("applytools")

_cdp_endpoint: str | None = None
_playwright = None
_browser = None


async def _get_page() -> Page:
    """The most recently opened page on the CDP-connected browser.

    Not page index 0 -- goose/the ATS may have opened new tabs (SSO
    redirects, a "review application" popup), and the one it's actually
    looking at is whichever was opened or focused last.
    """
    global _playwright, _browser
    if _playwright is None:
        _playwright = await async_playwright().start()
    if _browser is None or not _browser.is_connected():
        _browser = await _playwright.chromium.connect_over_cdp(_cdp_endpoint)
    for context in reversed(_browser.contexts):
        pages = context.pages
        if pages:
            return pages[-1]
    raise RuntimeError("no open page found on the CDP-connected browser")


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


async def _locate_combobox(page: Page, label_or_selector: str):
    """Best-effort trigger locator: label, then placeholder, then visible
    text -- each tried against the label as given and with a trailing
    required-marker stripped (see `_label_variants`) -- then a raw CSS
    selector as the last resort."""
    for text in _label_variants(label_or_selector):
        candidates = [
            page.get_by_label(text),
            page.get_by_placeholder(text),
            page.get_by_text(text, exact=False),
        ]
        for locator in candidates:
            try:
                if await locator.count() > 0:
                    return locator.first
            except Exception:
                continue
    try:
        locator = page.locator(label_or_selector)
        if await locator.count() > 0:
            return locator.first
    except Exception:
        pass
    return None


@mcp.tool()
async def fill_searchable_combobox(label_or_selector: str, value: str) -> str:
    """Fill a searchable/custom combobox in one deterministic call.

    Opens the combobox, types ``value`` into the filter input that appears,
    waits for a matching option, and clicks it. Replaces the improvised
    open-then-type-then-click sequence agents otherwise re-derive per ATS
    (custom dropdown widgets vary by vendor but this pattern is consistent).

    Args:
        label_or_selector: Visible label text, placeholder text, or a raw
            CSS selector for the combobox's clickable trigger element.
        value: Text to type into the filter and match against options.

    Returns a short ``ok: ...`` / ``error: ...`` status string.
    """
    page = await _get_page()
    trigger = await _locate_combobox(page, label_or_selector)
    if trigger is None:
        return f"error: could not locate a combobox trigger for {label_or_selector!r}"
    try:
        await trigger.click(timeout=5000)
        await page.keyboard.type(value, delay=40)
        await page.wait_for_timeout(400)
        option = page.get_by_role("option", name=value, exact=False).first
        await option.wait_for(state="visible", timeout=5000)
        option_text = (await option.text_content() or value).strip()
        await option.click(timeout=5000)
    except Exception as exc:
        return f"error: combobox fill failed for {label_or_selector!r} -> {value!r}: {exc}"
    return f"ok: selected {option_text!r} in combobox {label_or_selector!r}"


@mcp.tool()
async def read_field(label_or_selector: str) -> str:
    """Read one field's current value/state without a full-page snapshot.

    A `browser_snapshot` after every fill/click to confirm it landed dumps
    the whole page's accessibility tree -- tens of thousands of tokens, none
    of it cacheable, just to check one field. This targets the one element
    instead: same label/placeholder/selector resolution as
    `fill_searchable_combobox`, returning only its value/checked state and
    any validation-error text sitting next to it.

    Args:
        label_or_selector: Visible label text, placeholder text, or a raw
            CSS selector for the field to inspect.

    Returns a short ``ok: <value>`` / ``error: ...`` status string, e.g.
    ``ok: value="Jane Doe" invalid=false`` or
    ``ok: checked=true`` for a checkbox/radio.
    """
    page = await _get_page()
    locator = await _locate_combobox(page, label_or_selector)
    if locator is None:
        return f"error: could not locate a field for {label_or_selector!r}"
    try:
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
                        # SyntaxError, taking down the whole read_field call
                        # over what should just be a missing error message.
                        err_el = page.locator(f'[id="{id_}"]')
                        if await err_el.count() > 0:
                            err_text = (await err_el.first.text_content() or "").strip()
                            if err_text:
                                parts.append(f'error_text="{err_text[:200]}"')
                    except Exception:
                        continue
    except Exception as exc:
        return f"error: read failed for {label_or_selector!r}: {exc}"
    return "ok: " + " ".join(parts)


@mcp.tool()
async def check_for_errors() -> str:
    """Scan the current page for validation errors without a full snapshot.

    Looks only at the standard places a form puts an error: elements with
    role="alert", aria-invalid="true", or a class containing "error"/
    "invalid". Returns just those (name + message), which is normally a
    handful of short lines, instead of a full `browser_snapshot` whose whole
    purpose was to spot exactly this after a failed submit.

    Returns ``ok: no errors found`` or a newline-separated list of
    ``field: message`` (or ``message`` alone when no associated field name
    is found), capped at 15 entries.
    """
    page = await _get_page()
    try:
        found = await page.evaluate("""
            () => {
                const seen = new Set();
                const out = [];
                const nodes = document.querySelectorAll(
                    '[role="alert"], [aria-invalid="true"], [class*="error" i], [class*="invalid" i]'
                );
                for (const el of nodes) {
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
        return f"error: could not locate a clickable element for {text_or_selector!r}"
    try:
        await locator.click(timeout=5000)
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
                        await el.click(timeout=3000)
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


def main() -> None:
    global _cdp_endpoint
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-endpoint", required=True)
    args = parser.parse_args()
    _cdp_endpoint = args.cdp_endpoint
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
