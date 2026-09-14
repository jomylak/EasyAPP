"""Detail page enrichment: scrapes full descriptions and apply URLs.

For each job URL in the database, navigates to the detail page and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first):
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (1 LLM call)
"""

import json
import logging
import random
import re
import socket
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.database import init_db
from applypilot.llm import get_client

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Sites that block scraping -- skip detail extraction entirely
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from applypilot.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

def _load_base_urls() -> dict[str, str | None]:
    """Load site base URLs from config/sites.yaml."""
    from applypilot.config import load_base_urls
    return load_base_urls()


def resolve_url(raw_url: str, site: str) -> str | None:
    """Resolve a stored URL to an absolute URL."""
    if not raw_url:
        return None

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if site == "WelcomeToTheJungle":
        return None

    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"

    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None

    base = _load_base_urls().get(site)
    if not base:
        return None

    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]

    return urljoin(base, raw_url)


def resolve_all_urls(conn: sqlite3.Connection) -> dict:
    """Resolve all relative URLs in the database. Returns stats."""
    rows = conn.execute("SELECT url, site FROM jobs").fetchall()
    resolved = 0
    failed = 0
    already_absolute = 0

    for row in rows:
        url, site = row[0], row[1]
        if url.startswith("http://") or url.startswith("https://"):
            already_absolute += 1
            continue

        new_url = resolve_url(url, site)
        if new_url and new_url != url:
            try:
                conn.execute("UPDATE jobs SET url = ? WHERE url = ?", (new_url, url))
                resolved += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
                resolved += 1
        else:
            failed += 1

    # Also resolve relative application_urls
    app_resolved = 0
    rows = conn.execute(
        "SELECT url, site, application_url FROM jobs "
        "WHERE application_url IS NOT NULL AND application_url != '' "
        "AND application_url NOT LIKE 'http%'"
    ).fetchall()
    for row in rows:
        url, site, app_url = row[0], row[1], row[2]
        new_app = resolve_url(app_url, site)
        if new_app and new_app != app_url:
            conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?", (new_app, url))
            app_resolved += 1

    conn.commit()
    return {"resolved": resolved, "failed": failed, "already_absolute": already_absolute,
            "app_resolved": app_resolved}


def resolve_wttj_urls(conn: sqlite3.Connection) -> int:
    """Re-fetch WTTJ Algolia API to get proper detail URLs and fix slug-as-title.
    Returns count of URLs updated."""
    wttj_jobs = conn.execute(
        "SELECT url, title FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchall()

    if not wttj_jobs:
        return 0

    algolia_data: dict = {}

    def capture_algolia(response):
        if "algolia.net" in response.url and "/queries" in response.url:
            try:
                algolia_data["response"] = json.loads(response.text())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        page.on("response", capture_algolia)
        page.goto(
            "https://www.welcometothejungle.com/en/jobs?query=developer&refinementList%5Bremote%5D%5B%5D=fulltime",
            timeout=60000,
        )
        page.wait_for_load_state("networkidle")
        browser.close()

    if not algolia_data.get("response"):
        log.warning("WTTJ: No Algolia response captured")
        return 0

    results = algolia_data["response"].get("results", [])
    slug_map: dict = {}
    for rs in results:
        for hit in rs.get("hits", []):
            slug = hit.get("slug", "")
            org = hit.get("organization", {})
            org_slug = org.get("slug", "") if isinstance(org, dict) else ""
            name = hit.get("name", "")
            if slug and org_slug:
                detail_url = f"https://www.welcometothejungle.com/en/companies/{org_slug}/jobs/{slug}"
                slug_map[slug] = {"url": detail_url, "name": name}

    updated = 0
    for row in wttj_jobs:
        old_url, old_title = row[0], row[1]
        slug = old_url.split("_DFNS_")[0] if "_DFNS_" in old_url else old_url
        match = slug_map.get(slug) or slug_map.get(old_url)
        if match:
            try:
                conn.execute(
                    "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                    (match["url"], match["name"] or old_title, old_url),
                )
                updated += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                updated += 1
        else:
            for s, data in slug_map.items():
                if s in old_url or old_url in s:
                    try:
                        conn.execute(
                            "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                            (data["url"], data["name"] or old_title, old_url),
                        )
                        updated += 1
                    except sqlite3.IntegrityError:
                        conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                        updated += 1
                    break

    conn.commit()
    return updated


# -- Detail page intelligence ------------------------------------------------

_JOBRIGHT_NEXT_DATA_RE = re.compile(
    r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>', re.S
)


def _extract_jobright_publish_time(page) -> str | None:
    """Pull Jobright's own precise posting time off its job page, free.

    This page is already loaded here (scrape_detail_page navigates to it
    first, before ever clicking through to the employer's site), so this
    is a zero-extra-request safety net for whatever discovery's own fetch
    (_fetch_jobright_publish_time in discovery/smartextract.py) missed --
    same field, same reasoning: the bulk minisite API's own `postedAt` runs
    ~7h behind what Jobright's page itself displays, but the page's
    embedded Next.js data has the correct `publishTime`.
    """
    try:
        match = _JOBRIGHT_NEXT_DATA_RE.search(page.content())
        if not match:
            return None
        data = json.loads(match.group(1))
        publish_time = data["props"]["pageProps"]["dataSource"]["jobResult"].get("publishTime")
        if not publish_time:
            return None
        return datetime.fromisoformat(publish_time).replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


def collect_detail_intelligence(page) -> dict:
    """Collect signals from a detail page. Lighter than discovery -- no API interception."""
    intel: dict = {"json_ld": [], "page_title": "", "final_url": ""}

    intel["page_title"] = page.title()
    intel["final_url"] = page.url

    for el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(el.inner_text())
            intel["json_ld"].append(data)
        except Exception:
            pass

    return intel


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def _normalize_employer_date(date_str: str | None) -> str | None:
    """Parse a JSON-LD `datePosted` value into ISO 8601, or None.

    Unlike discovery's `_normalize_posted_date` (which also handles relative
    text like "2 days ago" scraped off a card), schema.org's `datePosted` is
    always meant to be an absolute date/datetime already -- so this only
    needs a straight parse, not the relative-text branches. Still runs
    through dateutil rather than assuming strict ISO, since real-world sites
    are inconsistent about it (bare "2026-09-13" vs a full timestamp).
    """
    if not date_str or not date_str.strip():
        return None
    from dateutil import parser as dateutil_parser
    try:
        parsed = dateutil_parser.parse(date_str.strip())
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def extract_from_json_ld(intel: dict) -> dict | None:
    """Extract description, apply URL, and posting date from JSON-LD JobPosting.
    Returns {"full_description": str, "application_url": str|None,
    "employer_posted_date": str|None} or None."""

    def find_job_posting(data):
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    result = find_job_posting(item)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = find_job_posting(item)
                if result:
                    return result
        return None

    for ld in intel.get("json_ld", []):
        posting = find_job_posting(ld)
        if not posting:
            continue

        desc = posting.get("description", "")
        if not desc:
            continue

        desc_clean = clean_description(desc)
        if len(desc_clean) < 50 or is_aggregator_boilerplate(desc_clean):
            continue

        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")

        return {
            "full_description": desc_clean,
            "application_url": apply_url,
            "employer_posted_date": _normalize_employer_date(posting.get("datePosted")),
        }

    return None


# -- Tier 2: Deterministic pattern matching ----------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a[data-testid*="apply"]',
    'a[class*="apply"]',
    'a[aria-label*="pply"]',
    'button[data-testid*="apply"]',
    'a#apply_button',
    '.postings-btn-wrapper a',
    'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]',
    'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]',
    'a[class*="apply-btn"]',
    'a[class*="apply-button"]',
]

DESCRIPTION_SELECTORS = [
    '#job-description',
    '#job_description',
    '#jobDescriptionText',
    '.job-description',
    '.job_description',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-testid*="description"]',
    '[data-testid="job-description"]',
    '.posting-page .posting-categories + div',
    '#content .posting-page',
    '#app_body .content',
    '#grnhse_app .content',
    '.ashby-job-posting-description',
    '[class*="posting-description"]',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '[class*="job-content"]',
    '[class*="job-body"]',
    '[role="main"] article',
    'main article',
    'article[class*="job"]',
    '.job-posting-content',
]


def extract_apply_url_deterministic(page) -> str | None:
    """Try known CSS patterns for apply buttons/links."""
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href")
                if href and href != "#":
                    return href
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    parent_href = el.evaluate("el => el.parentElement?.querySelector('a')?.href || null")
                    if parent_href:
                        return parent_href
                    return page.url
        except Exception:
            continue

    try:
        links = page.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip().lower()
            if "apply" in text and len(text) < 50:
                href = link.get_attribute("href")
                if href and href != "#" and "javascript:" not in href:
                    return href
    except Exception:
        pass

    return None


def _extract_page_description(page) -> tuple[str | None, str | None]:
    """Best-effort (description, employer_posted_date) pull from a page
    already loaded in the browser -- JSON-LD first (free, structured, and
    the only source that can carry a posting date), falling back to the
    same deterministic CSS patterns used for a normal detail-page scrape
    for description alone (no employer date -- that's JSON-LD-only, this
    module has no CSS-based date fallback).

    This runs against the real employer/ATS page (see
    resolve_original_job_url), not Jobright's own wrapper page -- so a
    `datePosted` found here is the employer's own stated date, the most
    authoritative one available (see database._DAY_EXPR).
    """
    try:
        intel = collect_detail_intelligence(page)
        json_ld_result = extract_from_json_ld(intel)
        if json_ld_result and json_ld_result.get("full_description"):
            return json_ld_result["full_description"], json_ld_result.get("employer_posted_date")
    except Exception:
        pass
    try:
        return extract_description_deterministic(page), None
    except Exception:
        return None, None


def _dismiss_tour_modal(page) -> None:
    """Best-effort dismiss of Jobright's onboarding tour overlay.

    Seen live on the newer AIML/DE category boards (not the established SWE
    ones): a `div#___reactour` overlay (the react-tour library) sits on top
    of the page and intercepts pointer events, so the "Original Job Post" /
    Apply-button clicks below retry against it for their full 5s timeout and
    then give up -- across ~900 jobs that adds up to hours. Couldn't
    reproduce it live to pin down its exact close-button markup (the
    profile used to check it didn't trigger the tour), so this tries
    several generic, low-risk strategies rather than one guessed selector;
    each is a no-op if the overlay isn't there. Never raises -- this must
    never be the reason a resolution attempt fails.
    """
    try:
        if not page.query_selector("#___reactour"):
            return
    except Exception:
        return

    for selector in (
        '#___reactour button[aria-label="Close"]',
        '#___reactour [aria-label*="close" i]',
        # The one actually observed live, once logged in: Jobright's "Orion"
        # resume-tailoring tour tooltip, an `EXIT` button
        # (id="index_tour-exit-button-id__..."). Matched by text rather than
        # that id, which looks like a per-build CSS-module hash, not
        # something to pin to.
        '#___reactour button:has-text("Exit")',
        '#___reactour button:has-text("Skip")',
        '#___reactour button:has-text("Got it")',
        '#___reactour button:has-text("Done")',
        '#___reactour [class*="close" i]',
    ):
        try:
            btn = page.query_selector(selector)
            if btn:
                btn.click(timeout=1500)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue

    # No recognizable close button -- react-tour closes on Escape by
    # default, and clicking well outside the highlighted element's mask
    # dismisses a plain click-outside overlay.
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if not page.query_selector("#___reactour"):
            return
        page.mouse.click(5, 5)
        page.wait_for_timeout(300)
    except Exception:
        pass


def _resolve_via_original_job_post(page) -> tuple[str | None, str | None, str | None]:
    """Primary strategy: click Jobright's "Original Job Post" toolbar link.

    A single stable link present on every job page regardless of which Apply
    button variant that posting shows ("Apply Now" vs "Apply With Autofill"
    vs others not yet seen) -- one click straight to the real employer URL,
    no upsell dialog, no skip-link dance. Verified against two different
    jobs with two different Apply-button variants; both resolved correctly
    through this one link.

    Returns (resolved_url, description, employer_posted_date) -- description
    and employer_posted_date are a best-effort pull from the real employer's
    own page before closing it, since we're already there: the original
    posting is more likely to state salary than Jobright's own summary of it
    (pay-transparency law requires it on the original in many states;
    Jobright's paraphrase doesn't reliably carry it over) and to carry its
    own JSON-LD `datePosted`, and it costs nothing extra -- this tab was
    already being opened and closed to get the URL.
    """
    # A wait, not an instant query: the link is fine on a fully-rendered
    # page, but querying the instant scrape_detail_page's own navigation
    # settles was seen to miss it on a slower-rendering layout, silently
    # falling through to the Apply-flow strategy (or nothing) for a job that
    # did have this link, just not yet.
    # A Locator, not an ElementHandle from wait_for_selector -- the latter is
    # a snapshot reference to one DOM node, which _dismiss_tour_modal's own
    # interaction (right below) can detach by triggering a React re-render
    # (seen consistently once logged in, which surfaces a first-login "tour"
    # modal that didn't appear signed out). A Locator re-resolves the live
    # DOM at click time instead of clicking a now-stale handle.
    link = page.locator("text=Original Job Post").first
    try:
        link.wait_for(timeout=4000)
    except Exception:
        return None, None, None
    _dismiss_tour_modal(page)
    pages_before = set(page.context.pages)
    link.click(timeout=5000)
    page.wait_for_timeout(2000)
    new_pages = set(page.context.pages) - pages_before
    if new_pages:
        new_page = new_pages.pop()
        new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        resolved = new_page.url
        description, employer_posted_date = _extract_page_description(new_page)
        new_page.close()
        return resolved, description, employer_posted_date
    return None, None, None


def _resolve_via_apply_flow(page) -> tuple[str | None, str | None, str | None]:
    """Fallback strategy: the Apply button's own flow.

    Only reached if "Original Job Post" isn't present for some job layout
    this hasn't seen yet. Jobright shows different Apply-button labels on
    different postings ("Apply Now", "Apply With Autofill", possibly others)
    -- match on either seen so far rather than one exact string. Clicking it
    can surface a "Customize Your Resume" upsell dialog with an "Apply
    Without Customizing" skip link before the real navigation happens; not
    every posting shows it. See _resolve_via_original_job_post for why a
    description and employer_posted_date are captured here too.
    """
    apply_btn = None
    for el in page.query_selector_all("a, button"):
        text = (el.inner_text() or "").strip().lower()
        if text in ("apply now", "apply with autofill"):
            apply_btn = el
            break
    if not apply_btn:
        return None, None, None

    _dismiss_tour_modal(page)
    pages_before = set(page.context.pages)
    apply_btn.click(timeout=5000)
    page.wait_for_timeout(1200)

    skip = page.query_selector("text=Apply Without Customizing")
    if skip:
        skip.click(timeout=3000)

    page.wait_for_timeout(1500)
    new_pages = set(page.context.pages) - pages_before
    if new_pages:
        new_page = new_pages.pop()
        new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        resolved = new_page.url
        description, employer_posted_date = _extract_page_description(new_page)
        new_page.close()
        return resolved, description, employer_posted_date
    return None, None, None


def resolve_original_job_url(page, candidate_url: str | None) -> tuple[str | None, str | None, str | None]:
    """Get the real employer ATS URL (and a best-effort description and
    posting date from that page) behind a Jobright-wrapped job page.

    Jobright's own detail page is itself an aggregator wrapper -- its og:url
    and default Apply link both stay on jobright.ai, so ATS detection run
    against them always returns "aggregator (unresolved)" even though the
    real Workday/Greenhouse/etc. posting is one click away. Requires this
    page's browser context to be signed into a real Jobright account (see
    ENRICHMENT_PROFILE_DIR) -- logged out, every path here hits a permanent
    signup wall with no way through.

    Tries the "Original Job Post" link first (simpler, one click, works
    regardless of which Apply-button variant the posting shows), falling
    back to the Apply button's own flow only if that link isn't present.

    Returns (resolved_url, description, employer_posted_date) -- any of the
    three may be None. Best-effort on failure -- most jobs are not
    aggregator-wrapped, a logged-out context can't get past the wall at all,
    and a page layout neither strategy recognizes should not break
    enrichment -- but every failure is now logged rather than swallowed
    silently, since a silent failure here is exactly what let 128 jobright.ai
    rows in the live DB store the aggregator's own UI chrome as if it were
    the job posting (see is_aggregator_boilerplate).
    """
    from applypilot.ats import _AGGREGATORS

    if not candidate_url or not re.search(_AGGREGATORS, candidate_url.lower()):
        return None, None, None

    try:
        # scrape_detail_page navigates `page` to the job's own url, which for
        # an Intern List job is intern-list.com, not jobright.ai -- candidate
        # is only a string at that point until this navigates there.
        if page.url.split("?")[0] != candidate_url.split("?")[0]:
            page.goto(candidate_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

        resolved, description, employer_posted_date = _resolve_via_original_job_post(page)
        if not resolved:
            resolved, description, employer_posted_date = _resolve_via_apply_flow(page)
        if not resolved:
            log.warning("Could not resolve original posting behind aggregator: %s", candidate_url)
        return resolved, description, employer_posted_date
    except Exception as e:
        log.warning("Original-posting resolution errored for %s: %s", candidate_url, e)
        return None, None, None


def extract_description_deterministic(page) -> str | None:
    """Try known CSS patterns for the job description block."""
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) >= 100:
                    return clean_description(text)
        except Exception:
            continue

    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = """You are extracting job details from a single job posting page.

PAGE URL: {url}
PAGE TITLE: {title}

Find TWO things in the HTML below:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link

Rules:
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
- If you cannot find one, set it to null

Return ONLY valid JSON:
{{"full_description": "the complete job description text here", "application_url": "https://..." or null}}

No explanation, no markdown. Keep reasoning under 20 words.

HTML:
{content}"""


def extract_main_content(page) -> str:
    """Extract the main content area, stripped of navigation noise."""
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el:
                text_len = len(el.inner_text().strip())
                if text_len > 200:
                    html = el.inner_html()
                    if len(html) < 50000:
                        return clean_content_html(html)
        except Exception:
            continue

    try:
        html = page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, header, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.innerHTML;
            }
        """)
        return clean_content_html(html[:50000])
    except Exception:
        return ""


def clean_content_html(html: str) -> str:
    """Clean detail page HTML for LLM consumption."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs

    return str(soup)


def extract_with_llm(page, url: str) -> dict:
    """Send focused HTML to LLM for extraction. Fallback tier."""
    content = extract_main_content(page)
    if not content:
        return {"full_description": None, "application_url": None}

    title = ""
    try:
        title = page.title()
    except Exception:
        pass

    prompt = DETAIL_EXTRACT_PROMPT.format(
        url=url,
        title=title,
        content=content[:30000],
    )

    try:
        client = get_client()
        t0 = time.time()
        raw = client.ask(prompt, temperature=0.0, max_tokens=4096)
        elapsed = time.time() - t0
        log.info("LLM: %d chars in, %.1fs", len(prompt), elapsed)

        from applypilot.discovery.smartextract import extract_json
        result = extract_json(raw)
        desc = result.get("full_description")
        apply_url = result.get("application_url")

        if desc:
            desc = clean_description(desc)

        return {"full_description": desc, "application_url": apply_url}
    except Exception as e:
        log.error("LLM ERROR: %s", e)
        return {"full_description": None, "application_url": None}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# Jobright.ai's own JSON-LD occasionally carries this promotional blurb --
# "AI Tools / Customize Your Resume / Maximize your interview chances /
# Build Cover Letter / Make your application stand out / Analyze How Well
# You Fit / Understand your strength & weakness" -- as the JobPosting
# `description` field itself, instead of the actual posting. It's short
# enough (174 chars) to pass extract_from_json_ld's `len(desc_clean) < 50`
# floor, so Tier 1 reported "ok" on 128 jobs in the live DB with no real
# content at all: no company, no real requirements, and a fit_score computed
# against nothing. Matched on a couple of its more distinctive phrases --
# ones a real job posting has no reason to contain -- rather than the whole
# string, so a near-identical repeat with different whitespace still catches.
_JOBRIGHT_BOILERPLATE_MARKERS = (
    "maximize your interview chances",
    "understand your strength & weakness",
)


def is_aggregator_boilerplate(text: str | None) -> bool:
    """True if `text` is the aggregator's own UI chrome, not a real posting."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _JOBRIGHT_BOILERPLATE_MARKERS)


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}

# Substrings of Playwright/Chromium error messages that mean "the machine has
# no usable network path right now" (wifi off, laptop asleep/closed, DNS
# server unreachable) rather than "this particular page/site is broken".
# Matched case-insensitively against result["error"].
NETWORK_DOWN_MARKERS = (
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_connection_reset",
    "err_connection_closed",
    "err_connection_refused",
    "err_connection_timed_out",
    "err_timed_out",
    "err_network_changed",
    "err_network_io_suspended",
    "err_address_unreachable",
    "err_proxy_connection_failed",
    "err_socket_not_connected",
    "err_empty_response",
)


def is_network_down_error(err: str | None) -> bool:
    """True if `err` looks like a local connectivity outage, not a bad page."""
    if not err:
        return False
    low = err.lower()
    return any(marker in low for marker in NETWORK_DOWN_MARKERS)


def wait_for_connectivity(max_wait: float = 1800.0, check_every: float = 10.0) -> bool:
    """Block until DNS/internet is reachable again, or `max_wait` elapses.

    Used as a circuit breaker when the batch loop hits a network-down error:
    without this, a tight `while pending: applypilot run enrich` loop (see
    scripts/overnight_pipeline.sh) burns through every job's retry budget in
    seconds while offline, since a DNS failure returns almost instantly.
    Polling here instead pauses the whole batch until the network is back,
    so no job's attempt count gets spent on an outage that had nothing to do
    with the job itself.
    """
    deadline = time.time() + max_wait
    waited = 0.0
    while time.time() < deadline:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=5).close()
            if waited:
                log.info("Connectivity restored after %.0fs, resuming enrichment.", waited)
            return True
        except OSError:
            time.sleep(check_every)
            waited += check_every
            if waited and waited % 60 < check_every:
                log.warning("Still offline after %.0fs, waiting for connectivity...", waited)
    log.error("Still offline after %.0fs (giving up on this wait, will retry job normally).", max_wait)
    return False


def _finalize_detail_result(result: dict, page, t0: float, url: str) -> dict:
    """Attach elapsed time, resolving an aggregator's real ATS URL first.

    Shared by every successful exit from scrape_detail_page so the click-
    through only needs to be written once. Falls back to the page's own url
    when no application_url was extracted -- for an aggregator-sourced job
    (Jobright), that IS the aggregator wrapper page, and its own "Apply Now"
    is a JS button with no href, so extract_apply_url_deterministic never
    finds a candidate to hand to the resolver in the first place. Without
    this fallback, resolve_original_job_url never even gets called.
    """
    candidate = result.get("application_url") or url

    resolved, original_description, employer_posted_date = resolve_original_job_url(page, candidate)
    if resolved:
        result["application_url"] = resolved
        if result.get("full_description"):
            result["status"] = "ok"

        # The employer's own posting is more likely to state salary than
        # Jobright's paraphrase of it (pay-transparency law requires it on
        # the original in many states; the summary doesn't reliably carry it
        # over). Prefer it whenever it's richer than what we already have --
        # this came from a page we were already opening and closing to get
        # the URL, so capturing it costs nothing extra.
        if original_description and len(original_description) > len(result.get("full_description") or ""):
            result["full_description"] = original_description
            result["status"] = "ok"

        # Same reasoning as the description above: the employer's own
        # JSON-LD `datePosted`, straight from the source, outranks anything
        # Jobright or a discovery-time scrape ever recorded (see
        # database._DAY_EXPR) -- store it whenever this resolution found one.
        if employer_posted_date:
            result["employer_posted_date"] = employer_posted_date

    result["elapsed"] = time.time() - t0
    return result


def scrape_detail_page(page, url: str) -> dict:
    """Full cascade for one detail page."""
    result: dict = {
        "full_description": None,
        "application_url": None,
        "employer_posted_date": None,
        "posted_date": None,
        "status": "error",
        "tier_used": None,
        "error": None,
    }
    t0 = time.time()

    try:
        resp = page.goto(url, timeout=45000)
        if resp and resp.status in PERMANENT_FAILURES:
            result["error"] = f"HTTP {resp.status}"
            result["elapsed"] = time.time() - t0
            return result
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception as e:
        err_str = str(e)
        if "timeout" in err_str.lower():
            result["error"] = "timeout"
        else:
            result["error"] = err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    if "jobright.ai/jobs/info/" in url:
        result["posted_date"] = _extract_jobright_publish_time(page)

    intel = collect_detail_intelligence(page)

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        return _finalize_detail_result(result, page, t0, url)

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(page)
    if is_aggregator_boilerplate(desc):
        desc = None
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        return _finalize_detail_result(result, page, t0, url)

    tier2_apply = apply

    # Tier 3: LLM
    llm_result = extract_with_llm(page, url)
    llm_desc = llm_result.get("full_description")
    result["full_description"] = None if is_aggregator_boilerplate(llm_desc) else llm_desc
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    return _finalize_detail_result(result, page, t0, url)


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 2.0,
    max_jobs: int | None = None,
    remote_report: Callable[[str, dict], None] | None = None,
    jitter: float = 0.0,
) -> dict:
    """Process all jobs for one site using shared browser context.

    If conn is None and remote_report is None, creates its own DB connection.

    remote_report, when given, is called once per job with (url, outcome)
    instead of every local `conn.execute(...)` in this function -- the
    mechanism that lets a machine with no direct DB access (e.g. the
    enrichment Pi, run from a home IP to get past Jobright's Cloudflare
    challenge on this VM's datacenter IP) reuse this exact scrape/retry/tier
    cascade unchanged, reporting results back over HTTP instead of writing
    SQL directly. `outcome` is one of:
      {"status": "success", "full_description": str|None, "application_url": str|None}
      {"status": "network_down", "error": str}
      {"status": "error", "error": str}
    The receiving end (queries.report_enrich_result) mirrors this function's
    own local UPDATE logic byte-for-byte, including the attempts/gave-up
    accounting and the post-success dedup check.

    jitter (0.0-1.0, default off) randomizes each inter-job sleep within
    delay*(1-jitter) to delay*(1+jitter) -- a fixed cadence for hours is a
    more obviously-mechanical shape than the same average rate with natural
    variance. Zero behavior change for existing callers, which never pass it.
    """
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    if max_jobs:
        jobs = jobs[:max_jobs]

    if not jobs:
        return stats

    own_conn = conn is None and remote_report is None
    if own_conn:
        conn = init_db()

    now = datetime.now(timezone.utc).isoformat()

    try:
        with sync_playwright() as p:
            launch_opts: dict = {"headless": True, "user_agent": UA}
            if _PROXY_CONFIG:
                launch_opts["proxy"] = _PROXY_CONFIG["playwright"]
            # Persistent, enrichment-only profile: a signed-in Jobright
            # session (set up once, manually, outside this pipeline) is what
            # lets resolve_original_job_url get past its signup wall. A fresh
            # throwaway context here would never carry that session.
            config.ENRICHMENT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(
                str(config.ENRICHMENT_PROFILE_DIR), **launch_opts
            )
            page = context.pages[0] if context.pages else context.new_page()

            for i, (url, title) in enumerate(jobs):
                log.info("[%d/%d] %s", i + 1, len(jobs), title[:50] if title else url[:50])

                result = scrape_detail_page(page, url)
                stats["processed"] += 1

                tier = result.get("tier_used")
                status = result["status"]
                elapsed = result.get("elapsed", 0)

                if tier:
                    stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                tier_str = f"T{tier}" if tier else "--"
                desc_len = len(result.get("full_description") or "")
                apply_str = "yes" if result.get("application_url") else "no"
                err_str = f" | err={result.get('error')}" if result.get("error") else ""

                log.info("  %s | %s | desc=%s chars | apply=%s | %.1fs%s",
                         status, tier_str, f"{desc_len:,}", apply_str, elapsed, err_str)

                if status in ("ok", "partial"):
                    stats[status] += 1
                    if remote_report:
                        remote_report(url, {
                            "status": "success",
                            "full_description": result.get("full_description"),
                            "application_url": result.get("application_url"),
                            "employer_posted_date": result.get("employer_posted_date"),
                            "posted_date": result.get("posted_date"),
                        })
                    else:
                        from applypilot.ats import detect_ats
                        detected = detect_ats(
                            result.get("application_url") or url,
                            result.get("full_description"),
                        )
                        conn.execute(
                            "UPDATE jobs SET full_description = ?, application_url = ?, "
                            "employer_posted_date = ?, posted_date = COALESCE(posted_date, ?), "
                            "detail_scraped_at = ?, detail_error = NULL, ats = ? WHERE url = ?",
                            (result.get("full_description"), result.get("application_url"),
                             result.get("employer_posted_date"), result.get("posted_date"),
                             now, detected, url),
                        )
                        conn.commit()
                        from applypilot.dedup import check_duplicate
                        dup = check_duplicate(conn, url)
                        if dup["duplicate_of"]:
                            log.info("  duplicate of %s (%s)", dup["duplicate_of"], dup["reason"])
                else:
                    stats["error"] += 1
                    err = result.get("error")

                    if is_network_down_error(err):
                        # This isn't the job's fault -- the machine itself
                        # has no network path right now (wifi off, laptop
                        # closed, DNS unreachable). Don't spend one of the
                        # job's 3 attempts on it, and don't mark it scraped:
                        # leave detail_attempts untouched so it's retried
                        # exactly as if this pass never happened. Block here
                        # until connectivity is back (or max_wait elapses)
                        # instead of racing through the rest of the batch --
                        # every remaining job would otherwise fail the same
                        # way in under a second each, burning attempts across
                        # the whole backlog for one outage.
                        if remote_report:
                            remote_report(url, {"status": "network_down", "error": err})
                        else:
                            conn.execute(
                                "UPDATE jobs SET detail_error = ? WHERE url = ?",
                                (err, url),
                            )
                            conn.commit()
                        if wait_for_connectivity():
                            continue
                        # Still offline after the long wait -- stop hammering
                        # this batch job-by-job (each would wait the same
                        # 30min again) and let the caller's outer loop
                        # (overnight/hourly pipeline script) retry the whole
                        # batch later instead.
                        log.error(
                            "Giving up on this batch (%d/%d done) -- still offline.",
                            i + 1, len(jobs),
                        )
                        break

                    if remote_report:
                        # Attempts/gave-up accounting happens server-side
                        # (queries.report_enrich_result) -- it reads and
                        # increments detail_attempts itself, mirroring the
                        # local branch below exactly. A remote reporter has
                        # no direct DB access to read the current count from.
                        remote_report(url, {"status": "error", "error": err or "unknown"})
                    else:
                        # detail_scraped_at is what takes a job out of the
                        # "pending" queue -- setting it unconditionally on every
                        # error used to permanently strand a job the moment it
                        # hit one bad network blip (net::ERR_INTERNET_DISCONNECTED,
                        # DNS failure, timeout), with no retry ever. Only give up
                        # for real after 3 attempts; before that, leave it NULL
                        # so the next enrichment pass picks it back up on its own,
                        # the same self-healing model scoring already uses.
                        # Read the count fresh from the DB rather than the `jobs`
                        # list passed in -- that list is built once per batch by
                        # two different callers (_run_detail_scraper, stream_detail),
                        # neither of which needs to carry this value through.
                        prev_attempts = conn.execute(
                            "SELECT detail_attempts FROM jobs WHERE url = ?", (url,)
                        ).fetchone()
                        attempts = ((prev_attempts[0] if prev_attempts else 0) or 0) + 1
                        if attempts >= 3:
                            conn.execute(
                                "UPDATE jobs SET detail_error = ?, detail_scraped_at = ?, "
                                "detail_attempts = ? WHERE url = ?",
                                (err or "unknown", now, attempts, url),
                            )
                        else:
                            conn.execute(
                                "UPDATE jobs SET detail_error = ?, detail_attempts = ? WHERE url = ?",
                                (err or "unknown", attempts, url),
                            )

                if not remote_report:
                    conn.commit()

                if i < len(jobs) - 1:
                    if jitter:
                        time.sleep(random.uniform(delay * (1 - jitter), delay * (1 + jitter)))
                    else:
                        time.sleep(delay)

            context.close()
    finally:
        if own_conn:
            conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    """Groups pending jobs by site and processes each batch.

    Sequential by default. When workers > 1, processes multiple site batches
    in parallel using ThreadPoolExecutor (each thread gets its own browser
    and DB connection).

    Returns aggregate stats dict.
    """
    skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
    where = f"WHERE detail_scraped_at IS NULL AND {skip_filter}"
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    log.info("Pending: %d jobs across %d sites (workers=%d)", len(rows), len(site_jobs), workers)
    for site, jobs in site_jobs.items():
        log.info("  %s: %d jobs", site, len(jobs))

    known_order = [
        "RemoteOK", "Job Bank Canada", "BuiltIn Remote",
        "WelcomeToTheJungle", "CareerJet Canada", "Hacker News Jobs",
    ]
    order = [s for s in known_order if s in site_jobs]
    order += [s for s in sorted(site_jobs.keys()) if s not in order]

    total_stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    def _merge_stats(stats: dict) -> None:
        for k in ("processed", "ok", "partial", "error"):
            total_stats[k] += stats[k]
        for t, count in stats["tiers"].items():
            total_stats["tiers"][t] = total_stats["tiers"].get(t, 0) + count

    if workers > 1 and len(order) > 1:
        # Parallel mode: each site batch runs in its own thread with its own
        # DB connection (conn=None tells scrape_site_batch to create one)
        def _scrape_site(site: str) -> dict:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)
            stats = scrape_site_batch(None, site, jobs, delay=delay, max_jobs=max_per_site)
            log.info("%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     site, stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))
            return stats

        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            futures = {pool.submit(_scrape_site, site): site for site in order}
            for future in as_completed(futures):
                _merge_stats(future.result())
    else:
        # Sequential mode (default)
        for site in order:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)

            stats = scrape_site_batch(conn, site, jobs, delay=delay, max_jobs=max_per_site)
            _merge_stats(stats)

            log.info("Site summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))

    log.info("TOTAL: %d processed | %d ok | %d partial | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["partial"], total_stats["error"])
    log.info("Tier distribution: T1=%d T2=%d T3=%d",
             total_stats["tiers"].get(1, 0), total_stats["tiers"].get(2, 0), total_stats["tiers"].get(3, 0))

    llm_calls = total_stats["tiers"].get(3, 0)
    total = total_stats["processed"]
    if total > 0:
        savings = ((total - llm_calls) / total) * 100
        log.info("LLM calls: %d/%d (%.0f%% saved)", llm_calls, total, savings)

    return total_stats


# -- Streaming detail scraper (for sequential pipeline) ----------------------

def stream_detail(
    upstream_done,
    my_done,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs, scrapes sites sequentially.

    Args:
        upstream_done: Event set when discover+extract done. None = run once.
        my_done: Event to set when this stage completes.
        proxy_str: Proxy in host:port:user:pass format.
        poll_interval: Seconds to sleep when no pending jobs found.
    """
    if proxy_str:
        set_proxy(proxy_str)

    conn = init_db()

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute",
             url_stats['resolved'], url_stats['already_absolute'])

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                f"WHERE detail_scraped_at IS NULL AND {skip_filter} "
                "ORDER BY site LIMIT 200"
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                for site, jobs in site_jobs.items():
                    delay = SITE_DELAYS.get(site, 2.0)
                    log.info("%s: %d jobs (delay=%.1fs)", site, len(jobs), delay)

                    try:
                        stats = scrape_site_batch(conn, site, jobs, delay=delay)
                        total_ok += stats["ok"] + stats["partial"]
                        total_err += stats["error"]
                        log.info("%s: %d ok, %d partial, %d error",
                                 site, stats['ok'], stats['partial'], stats['error'])
                    except Exception as e:
                        log.error("%s: CRASHED: %s", site, e)

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        if total_ok or total_err:
            log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        conn.close()
        my_done.set()


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 1) -> dict:
    """Main entry point for detail page enrichment.

    Fetches pending jobs from the database (those without full_description),
    resolves relative URLs, then runs the three-tier extraction cascade on
    each detail page.

    Args:
        limit: Maximum number of jobs per site to process.
        workers: Number of parallel threads for site batch processing. Default 1 (sequential).

    Returns:
        Dict with stats: processed, ok, partial, error, tiers.
    """
    conn = init_db()

    # URL resolution first
    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    # WTTJ special handling
    wttj_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()[0]
    if wttj_count > 0:
        sample = conn.execute(
            "SELECT url FROM jobs WHERE site = 'WelcomeToTheJungle' LIMIT 1"
        ).fetchone()
        if sample and not sample[0].startswith("http"):
            updated = resolve_wttj_urls(conn)
            log.info("WTTJ: %d URLs updated", updated)

    # Run the detail scraper
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)

    return stats


def requeue_boilerplate_rows(conn: sqlite3.Connection | None = None) -> int:
    """Find jobs whose stored full_description is actually the aggregator's
    own UI chrome (see is_aggregator_boilerplate) and reset them back into
    the enrichment queue.

    Only clears detail_scraped_at/full_description/detail_attempts -- fit_score
    etc. are left alone here; a subsequent `applypilot run score` (or
    `rescore-stale`) naturally re-scores once real content lands, same as
    any other row that comes back from enrichment with something new. This
    just gets these 128-and-counting rows out of the "already scraped"
    state so the next `applypilot run enrich` picks them back up -- with the
    boilerplate-rejection fix in place, that pass now has a real chance of
    reaching the actual posting instead of storing the same chrome again.
    """
    own_conn = conn is None
    conn = conn or init_db()
    rows = conn.execute(
        "SELECT url, full_description FROM jobs WHERE full_description IS NOT NULL"
    ).fetchall()
    urls = [r["url"] for r in rows if is_aggregator_boilerplate(r["full_description"])]
    if urls:
        placeholders = ",".join("?" * len(urls))
        conn.execute(
            f"UPDATE jobs SET detail_scraped_at = NULL, full_description = NULL, "
            f"detail_attempts = 0, detail_error = NULL WHERE url IN ({placeholders})",
            urls,
        )
        conn.commit()
    if own_conn:
        conn.close()
    return len(urls)
