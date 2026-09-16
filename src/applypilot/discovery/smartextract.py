"""AI-powered smart extraction: discovers jobs from arbitrary websites.

Two-phase approach:
  Phase 1: Lightweight intelligence (JSON-LD, API responses, data-testids, DOM stats)
           -> LLM picks the best extraction strategy
  Phase 2: Only for CSS selectors -- Playwright finds repeating card elements,
           extracts 2-3 examples, sends focused HTML to LLM for selector generation.

JSON-LD and API strategies execute directly from stored data -- no LLM needed.

Sites are loaded from config/sites.yaml, with {query_encoded} and {location_encoded}
placeholders replaced from the user's search configuration.
"""

import json
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import httpx
import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import init_db, get_stats, get_connection
from applypilot.dedup import canonicalize_url, find_exact_text_duplicate, normalize_location
from applypilot.llm import get_client

log = logging.getLogger(__name__)

# Fix Windows encoding -- prevents charmap errors on emoji/unicode in job titles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Known analytics/telemetry domains -- never job data, not worth an LLM judge
# call. Filtered out before capture rather than after, to save API quota.
_TELEMETRY_DOMAINS = (
    "amplitude.com", "segment.io", "segment.com", "google-analytics.com",
    "googletagmanager.com", "doubleclick.net", "hotjar.com", "sentry.io",
    "fullstory.com", "mixpanel.com", "intercom.io", "facebook.com/tr",
    "clarity.ms", "bugsnag.com", "datadoghq.com", "newrelic.com",
    "airtable.com/internal", "cloudflareinsights.com", "posthog.com",
    "heap.io", "logrocket.com", "clickcease.com", "hs-analytics.net",
)


# -- Location filtering -------------------------------------------------------

def _load_location_filter(search_cfg: dict | None = None):
    """Load location accept/reject lists from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter."""
    if not location:
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True
    for r in reject:
        if r.lower() in loc:
            return False
    for a in accept:
        if a.lower() in loc:
            return True
    return False


# -- Site configuration from YAML --------------------------------------------

def load_sites() -> list[dict]:
    """Load scraping target sites from config/sites.yaml."""
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        log.warning("sites.yaml not found at %s", path)
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("sites", [])


def _normalize_posted_date(posted_date: str | None) -> str | None:
    """Best-effort parse of a scraped posting-date string into ISO 8601.

    Handles relative formats ("2 days ago", "today", "yesterday") and
    absolute ones ("Aug 28, 2026", "08/28/2026"). Returns None when the text
    can't be parsed at all -- callers should fall back to discovered_at
    rather than treat that as "posted today", which would understate age.
    """
    if not posted_date or not posted_date.strip():
        return None

    from dateutil import parser as dateutil_parser

    text = posted_date.strip().lower()
    now = datetime.now(timezone.utc)

    relative_match = re.search(r"(\d+)\s*(hour|day|week)s?\s*ago", text)
    if relative_match:
        n, unit = int(relative_match.group(1)), relative_match.group(2)
        delta_days = n / 24 if unit == "hour" else n if unit == "day" else n * 7
        return (now - timedelta(days=delta_days)).isoformat()
    if "today" in text or "just posted" in text or "hour" in text:
        return now.isoformat()
    if "yesterday" in text:
        return (now - timedelta(days=1)).isoformat()

    try:
        parsed = dateutil_parser.parse(posted_date, fuzzy=True)
    except (ValueError, OverflowError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _posted_within_days(posted_date: str | None, days: int = 7) -> bool:
    """Check if a scraped posting date is within the last N days.

    Best-effort: dates come from LLM-extracted card text in all sorts of
    formats. If it can't be parsed at all, we keep the job rather than
    silently dropping it -- better to over-include than to zero out results
    because of a brittle date field on a page we've never scraped before.
    """
    if not posted_date or not posted_date.strip():
        return True

    normalized = _normalize_posted_date(posted_date)
    if normalized is None:
        return True

    age = datetime.now(timezone.utc) - datetime.fromisoformat(normalized)
    return age.days <= days


def _classify_job_type(site: str) -> str | None:
    """Derive internship vs new-grad from the source site name (config/sites.yaml)."""
    site_lower = site.lower()
    if "intern" in site_lower:
        return "internship"
    if "newgrad" in site_lower or "new grad" in site_lower or "new-grad" in site_lower:
        return "new_grad"
    return None


# Jobright's own "intern" category feed mixes in postings that are actually
# full new-grad roles (e.g. a title literally saying "New Grad 2027" or
# "... Graduate ... 2027 Start") -- _classify_job_type trusts the source site
# unconditionally, so those inherit "internship" with nothing to correct it.
# Titles are the one signal that disagrees, so check them too rather than
# trusting the site name alone. Deliberately conservative: any "intern"
# wording in the title wins, since a title can legitimately say both (e.g.
# "New Grad Internship Program").
_TITLE_NEW_GRAD_RE = re.compile(
    r"\bnew[\s-]?grad(?:uate)?\b"
    r"|\bgraduate\b(?!\s+(?:student|program|school|degree))",
    re.I,
)


def title_suggests_new_grad(title: str | None) -> bool:
    """Does this job title read as a new-grad role regardless of source site?"""
    if not title:
        return False
    if re.search(r"\bintern(?:ship)?\b", title, re.I):
        return False
    return bool(_TITLE_NEW_GRAD_RE.search(title))


def _store_jobs_filtered(
    conn: sqlite3.Connection,
    jobs: list[dict],
    site: str,
    strategy: str,
    accept_locs: list[str],
    reject_locs: list[str],
) -> tuple[int, int]:
    """Store jobs with location filtering. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    reposted = 0
    filtered = 0
    too_old = 0
    job_type = _classify_job_type(site)

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        if not _location_ok(job.get("location"), accept_locs, reject_locs):
            filtered += 1
            continue
        if not _posted_within_days(job.get("posted_date"), days=config.DEFAULTS["discovery_posted_within_days"]):
            too_old += 1
            continue
        row_job_type = job_type
        if row_job_type == "internship" and title_suggests_new_grad(job.get("title")):
            row_job_type = "new_grad"
        normalized_posted = _normalize_posted_date(job.get("posted_date"))

        url = canonicalize_url(url)
        location = normalize_location(job.get("location"))

        # Content-match check first: this is what catches Jobright reissuing
        # a fresh internal job id for the same posting on re-crawl (same
        # title/description/location, a genuinely different URL) -- the
        # IntegrityError branch below only catches a literal same-URL repost,
        # a different case (see its comment).
        if find_exact_text_duplicate(conn, job.get("title"), job.get("description"), location):
            existing += 1
            continue

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, job_type, posted_date, airtable_record_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 location, site, strategy, now, row_job_type,
                 normalized_posted,
                 job.get("airtable_record_id")),
            )
            new += 1
            # jobright_minisite_api rows deliberately arrive with
            # posted_date=None (see _scrape_jobright_minisite_api) since the
            # bulk API's own postedAt is wrong. Only fetch the real value
            # for genuinely new jobs -- one lightweight GET each, not one
            # per item in the ~4000-job list on every pass.
            if strategy == "jobright_minisite_api":
                job_id = url.rsplit("/", 1)[-1]
                publish_time = _fetch_jobright_publish_time(job_id)
                if publish_time:
                    conn.execute(
                        "UPDATE jobs SET posted_date = ? WHERE url = ?",
                        (publish_time, url),
                    )
        except sqlite3.IntegrityError:
            existing += 1
            # Jobright (and presumably other sources) re-touch a listing's
            # posted date when an employer renews/re-runs it -- same job_id,
            # newer postedAt. Bump our posted_date to match so the job
            # resurfaces under today's day-view bucket instead of staying
            # filed under whenever we first saw it, which is what makes a
            # real repost look identical to "we never re-checked this job."
            # Guarded to only move forward in time (never backward) so a
            # stale/unparsable posted_date on one pass can't regress a job
            # that already has a newer one recorded.
            if normalized_posted:
                cur = conn.execute(
                    "UPDATE jobs SET posted_date = ? WHERE url = ? "
                    "AND (posted_date IS NULL OR posted_date < ?)",
                    (normalized_posted, url, normalized_posted),
                )
                if cur.rowcount:
                    reposted += 1

    if filtered:
        log.info("Filtered %d jobs (wrong location)", filtered)
    if reposted:
        log.info("Bumped posted_date on %d reposted/renewed jobs", reposted)
    if too_old:
        log.info("Filtered %d jobs (posted more than %d days ago)",
                 too_old, config.DEFAULTS["discovery_posted_within_days"])
    conn.commit()
    return new, existing


# -- Page intelligence collector ---------------------------------------------

def collect_page_intelligence(url: str, headless: bool = True) -> dict:
    """Load a page with Playwright and collect every signal a scraping engineer
    would look at in DevTools. Returns a structured intelligence report."""
    intel: dict = {
        "url": url,
        "json_ld": [],
        "api_responses": [],
        "data_testids": [],
        "page_title": "",
        "dom_stats": {},
        "card_candidates": [],
    }

    captured_responses: list[dict] = []

    def on_response(response):
        ct = response.headers.get("content-type", "")
        rurl = response.url
        if any(ext in rurl for ext in [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", ".gif", ".webp"]):
            return
        if any(domain in rurl for domain in _TELEMETRY_DOMAINS):
            return
        if "json" in ct or "/api/" in rurl or "algolia" in rurl or "graphql" in rurl:
            try:
                body = response.text()
                try:
                    data = json.loads(body)
                except Exception:
                    data = None
                captured_responses.append({
                    "url": rurl,
                    "status": response.status,
                    "size": len(body),
                    "data": data,
                })
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=UA)
        page.on("response", on_response)

        page.goto(url, timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            # Some sites (continuous analytics/ad beacons) never go fully
            # idle. The page is already loaded via goto() above -- proceed
            # with whatever's rendered rather than failing the whole run.
            log.info("Page never reached networkidle within 15s, proceeding anyway: %s", url)

        intel["page_title"] = page.title()

        # 1. JSON-LD
        for el in page.query_selector_all('script[type="application/ld+json"]'):
            try:
                data = json.loads(el.inner_text())
                intel["json_ld"].append(data)
            except Exception:
                pass

        # 2. __NEXT_DATA__
        next_data = page.query_selector("script#__NEXT_DATA__")
        if next_data:
            try:
                intel["next_data"] = json.loads(next_data.inner_text())
            except Exception:
                pass

        # 3. data-testid attributes
        intel["data_testids"] = page.evaluate("""
            () => {
                const els = document.querySelectorAll('[data-testid]');
                const results = [];
                els.forEach(el => {
                    results.push({
                        testid: el.getAttribute('data-testid'),
                        tag: el.tagName.toLowerCase(),
                        text: el.innerText?.slice(0, 80) || ''
                    });
                });
                return results.slice(0, 50);
            }
        """)

        # 4. DOM stats
        intel["dom_stats"] = page.evaluate("""
            () => {
                const body = document.body;
                return {
                    total_elements: body.querySelectorAll('*').length,
                    links: body.querySelectorAll('a[href]').length,
                    headings: body.querySelectorAll('h1,h2,h3,h4').length,
                    lists: body.querySelectorAll('ul,ol').length,
                    tables: body.querySelectorAll('table').length,
                    articles: body.querySelectorAll('article').length,
                    has_data_ids: body.querySelectorAll('[data-id]').length,
                };
            }
        """)

        # 5. Find repeating card-like elements
        intel["card_candidates"] = page.evaluate("""
            () => {
                const candidates = [];
                const allParents = document.querySelectorAll('*');

                for (const parent of allParents) {
                    const children = Array.from(parent.children);
                    if (children.length < 3) continue;

                    const tagCounts = {};
                    children.forEach(c => {
                        const key = c.tagName;
                        tagCounts[key] = (tagCounts[key] || 0) + 1;
                    });

                    const dominant = Object.entries(tagCounts).sort((a,b) => b[1]-a[1])[0];
                    if (!dominant || dominant[1] < 3) continue;

                    const repeatingChildren = children.filter(c => c.tagName === dominant[0]);
                    const withText = repeatingChildren.filter(c => c.innerText?.trim().length > 20);
                    if (withText.length < 3) continue;

                    const withLinks = withText.filter(c => c.querySelector('a[href]'));
                    const score = withLinks.length * 2 + withText.length;

                    const parentId = parent.id ? '#' + parent.id : '';
                    const parentClasses = Array.from(parent.classList).filter(c => c.length < 30).slice(0, 3).join('.');
                    const parentTag = parent.tagName.toLowerCase();
                    const parentSelector = parentTag + (parentId || (parentClasses ? '.' + parentClasses : ''));

                    const childTag = dominant[0].toLowerCase();
                    const sampleChild = withText[0];
                    const childClasses = Array.from(sampleChild.classList).filter(c => c.length < 30).slice(0, 3).join('.');
                    const childSelector = childTag + (childClasses ? '.' + childClasses : '');

                    const examples = withText.slice(0, 3).map(c => {
                        const clone = c.cloneNode(true);
                        clone.querySelectorAll('script,style,svg,noscript').forEach(el => el.remove());
                        const html = clone.outerHTML;
                        return html.length > 5000 ? html.slice(0, 5000) + '...' : html;
                    });

                    candidates.push({
                        parent_selector: parentSelector,
                        child_selector: childSelector,
                        child_tag: childTag,
                        total_children: repeatingChildren.length,
                        with_text: withText.length,
                        with_links: withLinks.length,
                        score: score,
                        examples: examples,
                    });
                }

                candidates.sort((a,b) => b.score - a.score);
                return candidates.slice(0, 3);
            }
        """)

        # Capture full rendered HTML
        intel["full_html"] = page.content()

        browser.close()

    # Process API responses
    for resp in captured_responses:
        summary: dict = {
            "url": resp["url"][:200],
            "status": resp["status"],
            "size": resp["size"],
            "_raw_data": resp.get("data"),
        }
        data = resp.get("data")
        if data:
            if isinstance(data, list) and data:
                summary["type"] = f"array[{len(data)}]"
                if isinstance(data[0], dict):
                    summary["first_item_keys"] = list(data[0].keys())[:20]
                    summary["first_item_sample"] = {k: str(v)[:100] for k, v in list(data[0].items())[:8]}
            elif isinstance(data, dict):
                summary["type"] = "object"
                summary["keys"] = list(data.keys())[:20]

                def _explore_nested(obj, path_prefix, depth=0):
                    if depth > 3 or not isinstance(obj, dict):
                        return
                    for key in list(obj.keys())[:15]:
                        val = obj[key]
                        path = f"{path_prefix}.{key}" if path_prefix else key
                        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                            info = {
                                "count": len(val),
                                "first_item_keys": list(val[0].keys())[:20],
                                "first_item_sample": {k: str(v)[:200] for k, v in list(val[0].items())[:8]},
                            }
                            for subkey in list(val[0].keys())[:10]:
                                subval = val[0][subkey]
                                if isinstance(subval, list) and len(subval) > 0 and isinstance(subval[0], dict):
                                    info[f"first_item.{subkey}"] = {
                                        "count": len(subval),
                                        "first_item_keys": list(subval[0].keys())[:15],
                                        "first_item_sample": {k: str(v)[:100] for k, v in list(subval[0].items())[:8]},
                                    }
                                elif isinstance(subval, dict):
                                    info[f"first_item.{subkey}"] = {
                                        "type": "object",
                                        "keys": list(subval.keys())[:15],
                                        "sample": {k: str(v)[:150] for k, v in list(subval.items())[:8]},
                                    }
                            summary[f"nested_{path}"] = info
                        elif isinstance(val, dict) and depth < 3:
                            _explore_nested(val, path, depth + 1)
                _explore_nested(data, "")
        intel["api_responses"].append(summary)

    return intel


# -- Judge: filter API responses ---------------------------------------------

JUDGE_PROMPT = """You are filtering intercepted API responses from a job listings website.
Decide if this API response contains actual job listing data (titles, companies, locations, etc).

API Response Summary:
  URL: {url}
  Status: {status}
  Size: {size} chars
  Type: {type}
  Keys/Fields: {fields}
  Sample: {sample}

Is this job listing data? Answer in under 10 words. Return ONLY valid JSON:
{{"relevant": true, "reason": "job objects with title/company"}}
or
{{"relevant": false, "reason": "auth endpoint"}}

No explanation, no markdown, no thinking."""


def judge_api_responses(api_responses: list[dict]) -> list[dict]:
    """Use the LLM to filter API responses, keeping only job-relevant ones."""
    if not api_responses:
        return []

    client = get_client()
    relevant: list[dict] = []

    for resp in api_responses:
        fields = ""
        sample = ""
        resp_type = resp.get("type", "unknown")
        if "first_item_keys" in resp:
            fields = str(resp["first_item_keys"])
            sample = json.dumps(resp.get("first_item_sample", {}), indent=2)[:500]
        elif "keys" in resp:
            fields = str(resp["keys"])
            for k, v in resp.items():
                if k.startswith("nested_"):
                    fields += f"\n  .{k.replace('nested_', '')}: {v.get('count', '?')} items, keys={v.get('first_item_keys', '?')}"
                    sample = json.dumps(v.get("first_item_sample", {}), indent=2)[:500]
        else:
            fields = "no structured data"

        prompt = JUDGE_PROMPT.format(
            url=resp.get("url", "?")[:200],
            status=resp.get("status", "?"),
            size=resp.get("size", "?"),
            type=resp_type,
            fields=fields,
            sample=sample or "n/a",
        )

        try:
            raw = client.ask(prompt, temperature=0.0, max_tokens=1024)
            verdict = extract_json(raw)
            is_relevant = verdict.get("relevant", False)
            reason = verdict.get("reason", "?")
            log.info("Judge: %s -> %s (%s)", resp.get("url", "?")[:80],
                     "KEEP" if is_relevant else "DROP", reason)
            if is_relevant:
                relevant.append(resp)
        except Exception as e:
            log.warning("Judge ERROR for %s: %s -- keeping", resp.get("url", "?")[:80], e)
            relevant.append(resp)

    return relevant


# -- Phase 1: strategy selection ---------------------------------------------

def format_strategy_briefing(intel: dict) -> str:
    """Lightweight briefing for strategy selection. No raw DOM."""
    sections: list[str] = []
    sections.append(f"PAGE: {intel['url']}")
    sections.append(f"TITLE: {intel['page_title']}")

    # JSON-LD
    if intel["json_ld"]:
        job_postings = [j for j in intel["json_ld"] if isinstance(j, dict) and j.get("@type") == "JobPosting"]
        other = [j for j in intel["json_ld"] if not (isinstance(j, dict) and j.get("@type") == "JobPosting")]
        if job_postings:
            sections.append(f"\nJSON-LD: {len(job_postings)} JobPosting entries found (usable!)")
            sections.append(f"First JobPosting:\n{json.dumps(job_postings[0], indent=2)[:3000]}")
        else:
            sections.append("\nJSON-LD: NO JobPosting entries (json_ld strategy will NOT work)")
        if other:
            types = [j.get("@type", "?") if isinstance(j, dict) else "?" for j in other]
            sections.append(f"Other JSON-LD types (NOT job data): {types}")
    else:
        sections.append("\nJSON-LD: none")

    # API responses
    if intel["api_responses"]:
        sections.append(f"\nAPI RESPONSES INTERCEPTED: {len(intel['api_responses'])} calls")
        for resp in intel["api_responses"]:
            sections.append(f"\n  URL: {resp['url']}")
            sections.append(f"  Status: {resp['status']} | Size: {resp['size']:,} chars | Type: {resp.get('type', '?')}")
            if "first_item_keys" in resp:
                sections.append(f"  Item keys: {resp['first_item_keys']}")
                sections.append(f"  Sample: {json.dumps(resp.get('first_item_sample', {}), indent=2)[:1000]}")
            if "keys" in resp:
                sections.append(f"  Object keys: {resp['keys']}")
            for k, v in resp.items():
                if k.startswith("nested_"):
                    arr_name = k.replace("nested_", "")
                    sections.append(f"  .{arr_name}: array of {v['count']} items")
                    sections.append(f"    Item keys: {v['first_item_keys']}")
                    sections.append(f"    Sample: {json.dumps(v.get('first_item_sample', {}), indent=2)[:1000]}")
                    for sk, sv in v.items():
                        if sk.startswith("first_item.") and isinstance(sv, dict):
                            sub_name = sk.replace("first_item.", "")
                            if "count" in sv:
                                sections.append(f"    .{arr_name}[0].{sub_name}: array of {sv['count']} items")
                                sections.append(f"      Item keys: {sv['first_item_keys']}")
                                sections.append(f"      Sample: {json.dumps(sv.get('first_item_sample', {}), indent=2)[:1500]}")
                            elif "keys" in sv:
                                sections.append(f"    .{arr_name}[0].{sub_name}: object with keys {sv['keys']}")
                                sections.append(f"      Sample: {json.dumps(sv.get('sample', {}), indent=2)[:1500]}")
    else:
        sections.append("\nAPI RESPONSES: none intercepted")

    # data-testid
    if intel["data_testids"]:
        sections.append(f"\nDATA-TESTID ATTRIBUTES: {len(intel['data_testids'])} elements")
        for dt in intel["data_testids"][:15]:
            text_preview = dt['text'].replace('\n', ' ')[:60]
            sections.append(f"  <{dt['tag']} data-testid=\"{dt['testid']}\"> {text_preview}")
    else:
        sections.append("\nDATA-TESTID: none found")

    # DOM stats
    stats = intel.get("dom_stats", {})
    sections.append(f"\nDOM STATS: {stats.get('total_elements', '?')} elements, "
                    f"{stats.get('links', '?')} links, {stats.get('headings', '?')} headings, "
                    f"{stats.get('tables', '?')} tables, {stats.get('articles', '?')} articles, "
                    f"{stats.get('has_data_ids', '?')} data-id elements")

    # Card candidates
    if intel["card_candidates"]:
        sections.append(f"\nREPEATING ELEMENTS DETECTED: {len(intel['card_candidates'])} candidate groups")
        for i, cand in enumerate(intel["card_candidates"]):
            sections.append(f"  [{i}] parent={cand['parent_selector']} child={cand['child_selector']} "
                          f"count={cand['total_children']} with_text={cand['with_text']} with_links={cand['with_links']}")
    else:
        sections.append("\nREPEATING ELEMENTS: none detected")

    return "\n".join(sections)


STRATEGY_PROMPT = """You are analyzing a job listings page to pick the best extraction strategy.

Below is a lightweight intelligence briefing -- JSON-LD data, intercepted API responses, data-testid attributes, and DOM statistics. NO raw DOM HTML is included.

Pick the BEST strategy:

1. "json_ld" -- ONLY if briefing shows JobPosting JSON-LD entries (it will say "usable!")
2. "api_response" -- ONLY if an intercepted API response has job-like fields (name, title, salary, description, location, slug)
3. "css_selectors" -- when neither JSON-LD nor API data has job data

HOW TO THINK:
- If the briefing says "JSON-LD: NO JobPosting entries" or "json_ld strategy will NOT work", do NOT pick json_ld.
- For api_response: "url_pattern" must be a substring that matches one of the INTERCEPTED API URLs listed above (not the page URL!). Copy a unique part of the API URL.
- For api_response: "items_path" must point to the ARRAY of items, not a single item. Use dot notation with [n] ONLY for traversing into a specific index to reach an inner array. Example: if data is {{"results": [{{"hits": [...]}}]}}, items_path is "results[0].hits" to reach the hits array.
- For api_response: field paths (title, salary, etc.) are RELATIVE TO EACH ITEM in the array. If items are nested objects like {{"_source": {{"Title": "..."}}}}, use "_source.Title" for the title field.
- For css_selectors: just return {{"strategy":"css_selectors","reasoning":"...","extraction":{{}}}} -- selectors will be generated in a separate focused step.
- If the data includes a posted/published date (JSON-LD's "datePosted" is standard; API responses often have "created_at", "posted_at", "date", "published"), map it to "posted_date". Use null if there's genuinely no date field -- do not guess.

Return ONLY valid JSON:

For json_ld:
{{"strategy":"json_ld","reasoning":"...","extraction":{{"title":"title","salary":"baseSalary_path_or_null","description":"description","location":"jobLocation[0].address.addressCountry","url":"url_field","posted_date":"datePosted_or_null"}}}}

For api_response:
{{"strategy":"api_response","reasoning":"...","extraction":{{"url_pattern":"actual.url.substring","items_path":"path.to.the.array","title":"field_in_each_item","salary":"salary_field_or_null","description":"description_field_or_null","location":"location_path","url":"url_field","posted_date":"date_field_or_null"}}}}

For css_selectors:
{{"strategy":"css_selectors","reasoning":"...","extraction":{{}}}}

Keep reasoning under 20 words. No explanation, no markdown, no code fences.

INTELLIGENCE BRIEFING:
{briefing}"""


# -- Card HTML cleaning (allowlist approach) ----------------------------------

_ALLOWED_ATTRS = {"id", "href", "data-testid", "data-id", "data-type", "data-slug",
                  "role", "aria-label", "aria-labelledby", "type", "name", "for"}
_ALLOWED_PREFIXES = ("data-", "aria-")
_UTILITY_CLASS_RE = re.compile(
    r"^("
    r"[a-z]{1,2}-\d+|"
    r"[a-z]{1,3}-[a-z]{1,3}-\d+|"
    r"col-\d+|"
    r"d-\w+|"
    r"align-\w+|justify-\w+|"
    r"flex-\w+|order-\d+|"
    r"text-\w+|font-\w+|"
    r"bg-\w+|border-\w+|"
    r"rounded-?\w*|shadow-?\w*|"
    r"w-\d+|h-\d+|"
    r"position-\w+|overflow-\w+|"
    r"float-\w+|clearfix|"
    r"visible-\w+|invisible|"
    r"sr-only|"
    r"css-[a-z0-9]+|"
    r"sc-[a-zA-Z]+|"
    r"sc-[a-f0-9]+-\d+"
    r")$"
)


def clean_card_html(html: str) -> str:
    """Strip layout noise from card HTML, keep only what the LLM needs for selectors."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in _ALLOWED_ATTRS or any(attr.startswith(p) for p in _ALLOWED_PREFIXES):
                new_attrs[attr] = val
            elif attr == "class":
                classes = val if isinstance(val, list) else val.split()
                kept = [c for c in classes if not _UTILITY_CLASS_RE.match(c)]
                if kept:
                    new_attrs["class"] = kept
        tag.attrs = new_attrs

    return str(soup)


def clean_page_html(html: str, max_chars: int = 150_000) -> str:
    """Strip full page HTML to essential structure for LLM card detection."""
    soup = BeautifulSoup(html, "html.parser")

    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main and len(str(main)) > 1000:
        soup = BeautifulSoup(str(main), "html.parser")

    for tag in soup.find_all(["script", "style", "svg", "noscript", "iframe",
                              "link", "meta", "head", "footer", "nav"]):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in _ALLOWED_ATTRS or any(attr.startswith(p) for p in _ALLOWED_PREFIXES):
                new_attrs[attr] = val
            elif attr == "class":
                classes = val if isinstance(val, list) else val.split()
                kept = [c for c in classes if not _UTILITY_CLASS_RE.match(c)]
                if kept:
                    new_attrs["class"] = kept
        tag.attrs = new_attrs

    for tag in soup.find_all(True):
        if not tag.get_text(strip=True) and not tag.find("img") and not tag.find("a"):
            tag.decompose()

    result = str(soup)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n<!-- TRUNCATED -->"
    return result


# -- Phase 2: CSS selector generation ----------------------------------------

FULL_PAGE_SELECTOR_PROMPT = """You are a senior web scraping engineer. Below is the cleaned HTML of a job listings page.

Your task:
1. Find the repeating HTML elements that represent individual job listings
2. Generate CSS selectors to extract data from them

Return a JSON object:
- "job_card": CSS selector matching each job card (MUST match ALL cards on the page)
- "title": selector RELATIVE to the card for the job title
- "salary": selector relative to card for salary, or null
- "description": selector relative to card for description snippet, or null
- "location": selector relative to card for location, or null
- "url": selector relative to card for the link (<a> tag) to the job detail page
- "posted_date": selector relative to card for a posted/updated date or "X days ago" text, or null if the card doesn't show one

Selector rules:
- SIMPLEST wins. A single attribute selector like [data-testid="job-card"] is better than a multi-level path like li > div > [data-testid="job-card"]. Do NOT add parent/ancestor selectors unless the target is ambiguous without them.
- For data-testid/data-id with DYNAMIC values (e.g. data-testid="card-123"), use prefix matching: [data-testid^="card-"]
- For data-testid with STATIC values (e.g. data-testid="job-card"), use exact: [data-testid="job-card"]
- Prefer semantic HTML: article, section, h2, h3 over div
- NEVER use hashed/generated classes: sc-*, css-*, random 5-8 char strings like "fJyWhK"
- Max 2 levels deep. One level is best.
- The "url" selector should target an <a> element (we extract its href attribute)
- If the page has NO job listings visible, return {{"error": "no job listings found"}}

Return ONLY valid JSON, no explanation, no markdown.

PAGE HTML:
{page_html}"""


# -- LLM helpers -------------------------------------------------------------

def ask_llm(prompt: str) -> tuple[str, float, dict]:
    """Send prompt to LLM. Returns (response_text, seconds_taken, metadata)."""
    client = get_client()
    t0 = time.time()
    text = client.ask(prompt, temperature=0.0, max_tokens=4096)
    elapsed = time.time() - t0
    meta = {
        "finish_reason": "stop",
        "prompt_chars": len(prompt),
        "response_chars": len(text),
    }
    return text, elapsed, meta


def extract_json(text: str) -> dict:
    """Extract JSON from LLM response, handling think tags and code fences."""
    if "<think>" in text:
        after = text.split("</think>")[-1].strip()
        if after:
            text = after
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = text.strip()
    text = re.sub(r'\\([^"\\\/bfnrtu])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    while text.endswith("}") or text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            text = text[:-1].rstrip()
    raise json.JSONDecodeError("Could not parse JSON", text, 0)


# -- JSON path resolution ---------------------------------------------------

def resolve_json_path_raw(data, path: str):
    """Navigate a JSON path and return whatever is there (including lists/dicts)."""
    if not path or not data:
        return None
    try:
        current = data
        for part in path.replace("[", ".[").split("."):
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                current = current[idx]
            else:
                current = current[part]
        return current
    except (KeyError, IndexError, TypeError):
        return None


def resolve_json_path(data, path: str):
    """Simple JSON path resolver with type coercion for display."""
    if not path or not data:
        return None
    try:
        current = data
        for part in path.replace("[", ".[").split("."):
            if not part:
                continue
            if part.startswith("[") and part.endswith("]"):
                idx = int(part[1:-1])
                current = current[idx]
            else:
                current = current[part]
        if isinstance(current, (str, int, float)):
            return str(current) if not isinstance(current, str) else current
        elif isinstance(current, dict):
            return current.get("name", current.get("text", str(current)[:100]))
        elif isinstance(current, list):
            if current and isinstance(current[0], dict):
                return ", ".join(str(item.get("name", item.get("text", ""))) for item in current[:3])
            return ", ".join(str(x) for x in current[:3])
        return str(current) if current else None
    except (KeyError, IndexError, TypeError):
        return None


# -- Extraction executors ----------------------------------------------------

def execute_json_ld(intel: dict, plan: dict) -> list[dict]:
    """Extract jobs from JSON-LD JobPosting entries."""
    ext = plan["extraction"]
    jobs: list[dict] = []
    for entry in intel["json_ld"]:
        if not isinstance(entry, dict) or entry.get("@type") != "JobPosting":
            continue
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url", "posted_date"]:
            path = ext.get(field)
            if not path or path == "null":
                job[field] = None
                continue
            job[field] = resolve_json_path(entry, path)
        jobs.append(job)
    return jobs


def execute_api_response(intel: dict, plan: dict) -> list[dict]:
    """Extract jobs from intercepted API response data."""
    ext = plan["extraction"]
    url_pattern = ext.get("url_pattern", "")

    target_data = None
    for resp in intel["api_responses"]:
        if url_pattern in resp.get("url", ""):
            target_data = resp.get("_raw_data")
            break

    if not target_data:
        log.warning("Could not find stored API response matching: %s", url_pattern)
        return []

    items_path = ext.get("items_path", "")
    items = resolve_json_path_raw(target_data, items_path)
    if not isinstance(items, list):
        log.warning("items_path '%s' did not resolve to a list (got %s)", items_path, type(items).__name__)
        return []

    jobs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url", "posted_date"]:
            path = ext.get(field)
            if not path or path == "null":
                job[field] = None
                continue
            job[field] = resolve_json_path(item, path)
        jobs.append(job)
    return jobs


def execute_css_selectors(intel: dict) -> tuple[dict, list[dict]]:
    """Phase 2: Send full cleaned page HTML to LLM for card detection + selector generation.
    Returns (selectors, jobs)."""
    full_html = intel.get("full_html", "")
    if not full_html:
        log.warning("No page HTML captured")
        return {}, []

    cleaned = clean_page_html(full_html)
    log.info("Page HTML: %s -> %s chars", f"{len(full_html):,}", f"{len(cleaned):,}")

    prompt = FULL_PAGE_SELECTOR_PROMPT.format(page_html=cleaned)

    try:
        raw, elapsed, meta = ask_llm(prompt)
    except Exception as e:
        log.error("LLM_ERROR in Phase 2: %s", e)
        return {}, []

    log.info("Phase 2 LLM: %d chars, %.1fs", meta['response_chars'], elapsed)

    try:
        selectors = extract_json(raw)
    except Exception as e:
        log.error("PARSE_ERROR in Phase 2: %s | raw: %s", e, raw[:500])
        return {}, []

    if "error" in selectors:
        log.warning("LLM: %s", selectors["error"])
        return selectors, []

    log.info("Selectors: %s", selectors)

    # Apply selectors to the ORIGINAL full_html
    soup = BeautifulSoup(full_html, "html.parser")
    card_sel = selectors.get("job_card", "NONE")
    try:
        cards = soup.select(card_sel)
    except Exception as e:
        log.error("Invalid card selector '%s': %s", card_sel, e)
        return selectors, []

    log.info("Matched %d cards", len(cards))

    jobs: list[dict] = []
    for card in cards:
        job: dict = {}
        for field in ["title", "salary", "description", "location", "url", "posted_date"]:
            sel = selectors.get(field)
            if not sel or sel == "null":
                job[field] = None
                continue
            try:
                el = card.select_one(sel)
            except Exception:
                job[field] = None
                continue
            if el:
                job[field] = el.get("href") if field == "url" else el.get_text(strip=True)
            else:
                job[field] = None
        jobs.append(job)
    return selectors, jobs


_AIRTABLE_RECORD_ID_RE = re.compile(r"(rec[A-Za-z0-9]{14,})/?$")


def _extract_airtable_record_id(href: str | None) -> str | None:
    """Pull the trailing `rec...` record id off an expand-row href.

    e.g. ".../viwz0pLEKnH0F4Hno/recAmG8jEMzAf50nq" -> "recAmG8jEMzAf50nq".
    This id is readable straight off the unexpanded grid row (the href is
    already on the DOM's `[data-testid="expandRowWrapper"]` anchor) -- no
    click/expand needed -- and is stable across re-crawls, which is what lets
    _scrape_airtable_button_grid skip the ~0.5s expand-and-read cost for a
    row it has already resolved on a previous pass.
    """
    if not href:
        return None
    m = _AIRTABLE_RECORD_ID_RE.search(href)
    return m.group(1) if m else None


def _extract_dialog_field(full_text: str | None, label: str) -> str | None:
    """Pull one labeled field's value out of an expanded Airtable record's
    flat `inner_text()` dump.

    Every field but the primary one (Position Title, read positionally as
    the dialog's first line) renders as its label on its own line followed
    by its value on the next -- e.g. "...\\nDate\\n2026-09-13\\nApply\\n...".
    Returns None if the label isn't present or has nothing after it (a
    genuinely blank field, or the label is the dialog's last line).
    """
    if not full_text:
        return None
    lines = full_text.split("\n")
    for i, line in enumerate(lines[:-1]):
        if line.strip() == label:
            value = lines[i + 1].strip()
            return value or None
    return None


def _scrape_airtable_button_grid(
    url: str,
    headless: bool = True,
    known_record_ids: set[str] | None = None,
    on_job=None,
    stats: dict | None = None,
) -> list[dict]:
    """Bespoke scraper for Airtable-embedded job boards with a Button-field
    apply link (newgrad-jobs.com's structure, and the AIML/DE category tags
    on both newgrad-jobs.com and intern-list.com).

    The generic two-phase LLM strategy correctly finds row titles from the
    virtualized grid, but a Button field's real <a href> only exists in the
    DOM once a row is expanded into its record-detail modal -- the grid cell
    itself is a non-interactive rendering of the button, not a link. This
    expands each row, reads the href directly, and closes the modal before
    moving on: one extra step per row, but a stable direct DOM read instead
    of guessing at virtualized cell selectors that come and go with scroll.

    Full-grid pagination: Airtable's grid is canvas-rendered with a fixed-size
    pool of `[data-testid="expandRowWrapper"]` overlay elements (observed
    ~21-27 regardless of scroll position) that get REPOSITIONED to track
    whatever's currently visible -- it's not a hard cap on how much data
    exists, just how much is mounted at once. All ~600-4700 records are
    already loaded client-side (confirmed live: no network request fires on
    scroll), so getting the rest is purely a matter of scrolling the grid's
    own scroll container (`.antiscroll-inner.keyboard-accessible-grid-container`,
    found live -- NOT the page/iframe scroll) and re-reading that same pool
    of overlays at each position. Scrolls by one full viewport height per
    round (some overlap at the edges is expected and handled by `seen_urls`
    dedup) until scrollTop stops advancing. Verified live (2026-09): the
    scroll loop itself does reach the bottom of a 600+ record grid in ~25
    rounds -- it is not actually capped at the first screenful, contrary to
    an earlier (stale) assumption in config/sites.yaml. The real cost is
    time: expanding every row to read its Button-field href, one at a time,
    measured at ~0.5s/row live -- see `known_record_ids` below for how that's
    avoided on steady-state re-crawls.

    Args:
        known_record_ids: Record ids (see _extract_airtable_record_id)
            already stored for this site from a previous pass. Every row's
            href is read cheaply (no click) and checked against this set
            first -- a match skips the expand/read/close cycle entirely, so
            a re-crawl only pays the real per-row cost for genuinely new
            rows. On an already-caught-up board that turns a several-minute
            scrape into a ~10-20s scroll-through that expands nothing. None
            (or empty) expands every row, same as the original behavior.
        on_job: Optional callback invoked with each newly-resolved job dict
            the moment it's read, so the caller can store it in the database
            immediately instead of waiting for the whole grid to finish --
            the freshest posting (found first, since the grid reads newest-
            first) no longer sits unstored for the several minutes a full
            scrape of an uncached board can take.
        stats: Optional dict, populated with "skipped_known" and
            "rows_examined" (jobs found + skipped) counts. A steady-state
            re-crawl legitimately returns zero NEW jobs once caught up --
            the caller needs rows_examined, not just len(jobs), to tell that
            apart from the scrape having failed to read the grid at all.

    Deliberately extracts ONLY title and url. These rows link to jobright.ai
    detail pages -- the same backing source Intern List uses -- so the normal
    enrichment stage fills in full_description, real salary/location, and the
    resolved ATS the same way it already does for Intern List jobs. Trying to
    also parse salary/location/qualifications out of the modal's flat text
    here would be fragile (blank fields don't get a placeholder value line,
    so position-based pairing breaks) for data enrichment recovers anyway.
    """
    from playwright.sync_api import sync_playwright

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    seen_record_ids: set[str] = set(known_record_ids or ())
    skipped_known = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 900})
        try:
            page.goto(url, timeout=45000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            scroll_container = page.query_selector(
                ".antiscroll-inner.keyboard-accessible-grid-container"
            )
            total_label = page.query_selector(".selectionCount.summaryCell")
            log.info(
                "Airtable grid: %s (scroll container %s)",
                total_label.inner_text() if total_label else "record count unknown",
                "found" if scroll_container else "NOT FOUND -- only first-view rows will be read",
            )

            # Safety cap, not an expected stopping point -- a real board
            # tops out around 4700 records (Jobright's own ceiling), and one
            # viewport of virtualized rows is roughly 25-30, so 400 rounds
            # covers ~10-12k rows before this gives up.
            MAX_ROUNDS = 400
            stable_rounds = 0
            last_scroll_top = -1

            for round_num in range(MAX_ROUNDS):
                row_count = len(page.query_selector_all('[data-testid="expandRowWrapper"]'))
                for i in range(row_count):
                    opened_dialog = False
                    try:
                        expand_links = page.query_selector_all('[data-testid="expandRowWrapper"]')
                        if i >= len(expand_links):
                            break
                        target = expand_links[i]

                        # Cheap pre-check: the record id is already sitting on
                        # this row's own href, no click needed. A row we've
                        # already resolved (this pass or a previous one) is
                        # skipped entirely -- this is what makes a steady-
                        # state re-crawl fast instead of re-paying the ~0.5s
                        # expand cost for every one of hundreds of old rows.
                        record_id = _extract_airtable_record_id(target.get_attribute("href"))
                        if record_id and record_id in seen_record_ids:
                            skipped_known += 1
                            continue

                        target.scroll_into_view_if_needed(timeout=3000)
                        target.click(timeout=5000)
                        opened_dialog = True
                        page.wait_for_selector('[role="dialog"]', timeout=5000)
                        dialog = page.query_selector('[role="dialog"]')
                        if not dialog:
                            continue

                        # The record's primary field (Position Title for this
                        # base) is always the dialog's first line of text.
                        full_text = dialog.inner_text()
                        title = full_text.split("\n")[0].strip() if full_text else None
                        posted_date = _extract_dialog_field(full_text, "Date")

                        link_el = dialog.query_selector('a[data-button-field-button="true"]')
                        apply_url = link_el.get_attribute("href") if link_el else None

                        if record_id:
                            seen_record_ids.add(record_id)
                        if title and apply_url and apply_url not in seen_urls:
                            seen_urls.add(apply_url)
                            job = {
                                "title": title,
                                "url": apply_url,
                                "salary": None,
                                "description": None,
                                "location": None,
                                "posted_date": posted_date,
                                "airtable_record_id": record_id,
                            }
                            jobs.append(job)
                            if on_job:
                                on_job(job)
                    except Exception as e:
                        log.debug("Round %d row %d extraction failed: %s", round_num, i, e)
                    finally:
                        if not opened_dialog:
                            continue
                        # Escape leaves the dialog's outer container mounted in
                        # the DOM (a stale, invisible copy that intercepts the
                        # NEXT row's click), even though it looks fully closed --
                        # clicking the dialog's own close button does a complete
                        # teardown instead. Without this, every other row failed.
                        try:
                            dialog = page.query_selector('[role="dialog"]')
                            close_btn = dialog.query_selector(
                                'button[aria-label="Close"], [data-tutorial-selector-id="detailViewCloseButton"]'
                            ) if dialog else None
                            if close_btn:
                                close_btn.click(timeout=2000)
                            else:
                                page.keyboard.press("Escape")
                        except Exception:
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                        page.wait_for_timeout(300)

                if not scroll_container:
                    break

                scroll_top = scroll_container.evaluate(
                    "el => { const before = el.scrollTop; "
                    "el.scrollTop += el.clientHeight; "
                    "el.dispatchEvent(new Event('scroll', {bubbles: true})); "
                    "return el.scrollTop; }"
                )
                page.wait_for_timeout(400)
                log.info(
                    "Airtable grid: round %d done, %d unique rows so far (scrollTop=%s)",
                    round_num, len(jobs), scroll_top,
                )
                if scroll_top <= last_scroll_top:
                    stable_rounds += 1
                    if stable_rounds >= 2:
                        break
                else:
                    stable_rounds = 0
                last_scroll_top = scroll_top
        finally:
            browser.close()

    log.info("Airtable grid: extracted %d new rows with a usable url, skipped %d already-known",
              len(jobs), skipped_known)
    if stats is not None:
        stats["skipped_known"] = skipped_known
        stats["rows_examined"] = len(jobs) + skipped_known
    return jobs


# Matches the real request body Jobright's own embed widget sends (captured
# from live network traffic) -- keeps low-quality data-labeling/AI-training
# postings out before they ever reach our own filters.
_JOBRIGHT_EXCLUDE_TITLES = [
    "AI Trainer", "AI Tutor", "AI Training", "AI Coach", "AI Reviewer", "AI Rater",
    "AI Content Evaluator", "Search Quality Rater", "Ads Quality Rater", "Annotator",
    "Annotation Specialist", "Data Annotation", "AI Annotation", "Data Labeler",
    "Data Labeling", "Labeler", "AI Data Specialist", "Data Collector",
    "Data Collection", "Prompt Optimization", "Prompt Creator",
]


_JOBRIGHT_NEXT_DATA_RE = re.compile(
    r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>', re.S
)

# Set (to a wall-clock deadline) once this process sees a 303 challenge from
# the per-job page endpoint -- see _fetch_jobright_publish_time. Once
# tripped, every call short-circuits to None with zero network I/O until the
# deadline passes, instead of every new job in the batch separately paying
# for its own retries against a wall. Confirmed this endpoint's challenge
# can stay up for 20+ minutes under real conditions (far longer than the
# "recovers in seconds" seen in isolated single-request testing), and a
# batch of ~150 new jobs each burning ~9s of retries turned one discover
# pass into a 25-minute one and starved every other writer of the DB lock
# for that whole time -- this exists specifically to stop that.
_jobright_page_blocked_until = 0.0


def _fetch_jobright_publish_time(job_id: str, retries: int = 1) -> str | None:
    """Pull the precise, correct posting time for one Jobright job.

    The bulk `/swan/mini-sites/list` API's own `postedAt` field is wrong --
    spot-checked against Jobright's own job page and found running ~7h
    behind what that page itself displays (looks like a tz bug on
    Jobright's end, e.g. a Pacific-time value stored as if it were UTC).
    That job page, though, embeds a Next.js `__NEXT_DATA__` JSON blob with
    a `publishTime` field that matches its own displayed "X ago" text --
    confirmed by cross-checking `publishTimeDesc` (e.g. "8 hours ago")
    against `publishTime` for the same job. A plain GET here (no browser,
    no JS execution needed -- the value is already in the server-rendered
    HTML) is what backs both new-job discovery and backfilling existing
    rows.

    This endpoint has its own rate limiter, separate from the bulk API's:
    a 303 to `/_jr/security/challenge` (an actual Cloudflare Turnstile page,
    not a simple counter) once requests come in faster than ~1/2s. See
    _jobright_page_blocked_until: the first 303 in a process trips a 5-
    minute cooldown shared by every subsequent call, so one blocked job
    doesn't cost the next 150 their own retries too.
    """
    global _jobright_page_blocked_until
    if time.time() < _jobright_page_blocked_until:
        return None
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(
                f"https://jobright.ai/jobs/info/{job_id}",
                headers={"User-Agent": UA},
                timeout=15.0,
            )
            if resp.status_code == 303:
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                _jobright_page_blocked_until = time.time() + 300
                return None
            match = _JOBRIGHT_NEXT_DATA_RE.search(resp.text)
            if not match:
                return None
            data = json.loads(match.group(1))
            publish_time = data["props"]["pageProps"]["dataSource"]["jobResult"].get("publishTime")
            if not publish_time:
                return None
            parsed = datetime.fromisoformat(publish_time).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except Exception as e:
            log.warning("Jobright publishTime fetch failed for %s: %s", job_id, e)
            return None
    return None


def _scrape_jobright_minisite_api(category: str, on_job=None) -> list[dict]:
    """Pull jobs directly from Jobright's own public minisite JSON API.

    Found by watching network traffic on the embedded widget both
    Intern List and NewGrad Jobs use: `POST /swan/mini-sites/list` is what
    the widget itself calls to page through results as you scroll. No
    cookies or auth required -- confirmed with a bare curl POST, no browser
    session at all. Paginates cleanly by `position`/`count` up to the
    response's own `total`.

    This replaces BOTH the Jobright-iframe CSS scraper (Intern List) and the
    Airtable Button-field scraper (NewGrad Jobs): each of those was limited
    to whatever the widget rendered on its initial view (~20-30 rows) since
    neither drove real pagination, when the underlying dataset is actually
    ~3000-4700 jobs. It's also strictly richer data per job -- full
    qualifications text and a real salary field, for free, in the same
    call. NOT using this response's `postedAt` field, though: spot-checked
    it against Jobright's own job pages and it runs ~7h behind what their
    site actually displays for the same job (looks like a tz bug on their
    end). See the "posted_date": None below.

    Args:
        category: Jobright's own category slug, e.g. "intern:us:swe" or
            "newgrad:us:swe". Determines which board this pulls.
        on_job: Optional callback invoked with each job dict as its page
            comes back, so the caller can store it immediately rather than
            waiting for the whole (usually single-request) response to
            finish -- the earliest, freshest postings no longer sit unstored
            for however long the rest of the fetch takes.
    """
    jobs: list[dict] = []
    position = 0
    # Large enough to pull the whole list (observed range: ~3900-4700) in a
    # single request. Used to be 50, which meant ~80-95 sequential requests
    # per pass -- and this list isn't static while we page through it:
    # Jobright re-touches postedAt on existing listings continuously (an
    # employer renewal, apparently), so items shift position mid-crawl.
    # Confirmed missing a job that was live on the site because it drifted
    # across a page boundary between two of those ~80 requests. A single
    # request sees one consistent snapshot, so there's no boundary for
    # anything to drift across. The while loop below still repages if a
    # future `total` ever exceeds this, so nothing silently truncates if the
    # list outgrows it.
    count = 10000
    total: int | None = None
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Referer": "https://jobright.ai/",
        "Accept": "application/json",
    }
    body = {
        "category": category,
        "excludeTitle": _JOBRIGHT_EXCLUDE_TITLES,
        "excludedTitle": _JOBRIGHT_EXCLUDE_TITLES,
    }

    with httpx.Client(timeout=20.0) as client:
        while total is None or position < total:
            try:
                resp = client.post(
                    f"https://jobright.ai/swan/mini-sites/list?position={position}&count={count}",
                    headers=headers, json=body,
                )
                data = resp.json()
            except Exception as e:
                log.warning("Jobright minisite API request failed at position %d: %s", position, e)
                break

            if not data.get("success"):
                log.warning("Jobright minisite API error at position %d: %s",
                            position, data.get("errorMsg"))
                break

            result = data.get("result", {})
            batch = result.get("jobList", [])
            total = result.get("total", 0)
            if not batch:
                break

            for item in batch:
                props = item.get("properties", {}) or {}
                job_id = item.get("jobId")
                if not job_id:
                    continue
                salary = props.get("salary")
                if salary in (None, "N/A", ""):
                    salary = None

                job = {
                    "title": props.get("title"),
                    "salary": salary,
                    "description": props.get("qualifications"),
                    "location": props.get("location"),
                    "url": f"https://jobright.ai/jobs/info/{job_id}",
                    # Deliberately not using this item's "postedAt": spot-checked
                    # against Jobright's own job pages and it runs ~7h behind what
                    # their site displays (looks like a tz bug on their end, not
                    # ours). The real value (see _fetch_jobright_publish_time) gets
                    # filled in separately, right after a new row is inserted --
                    # see _store_jobs_filtered.
                    "posted_date": None,
                }
                jobs.append(job)
                if on_job:
                    on_job(job)

            position += count

    log.info("Jobright minisite API (%s): pulled %d of %d total jobs", category, len(jobs), total or 0)
    return jobs


# -- Main per-site extraction ------------------------------------------------

def _make_streaming_sink(
    conn: sqlite3.Connection, name: str, strategy: str,
    accept_locs: list[str], reject_locs: list[str], flush_every: int = 1,
):
    """Build an on_job callback that stores each job to the DB as a scraper
    finds it, instead of the caller buffering an entire site's results in
    memory and storing them all in one shot once the whole (possibly
    several-minutes-long) scrape finishes. Cheap to do per-job here: WAL
    mode + a 10s busy_timeout (see init_db()) make each commit fast and
    tolerant of the enrich/score loops writing to the same DB concurrently.
    Returns (on_job, flush, stats) -- call flush() once after the scraper
    returns to write out anything still sitting in the buffer (a no-op at
    flush_every=1 unless a caller raises it back up).
    """
    buffer: list[dict] = []
    stats = {"new": 0, "existing": 0}

    def flush() -> None:
        if not buffer:
            return
        n, e = _store_jobs_filtered(conn, buffer, name, strategy, accept_locs, reject_locs)
        stats["new"] += n
        stats["existing"] += e
        buffer.clear()

    def on_job(job: dict) -> None:
        buffer.append(job)
        if len(buffer) >= flush_every:
            flush()

    return on_job, flush, stats


def _run_one_site(
    name: str, url: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
) -> dict:
    """Run full smart extraction pipeline on one site URL.

    Args:
        accept_locs, reject_locs: When given (not None), the two fast-path
            scrapers below (Jobright minisite API, Airtable button-grid)
            store each job to the database as they find it rather than
            returning one big list for the caller to store after the whole
            site finishes -- see _make_streaming_sink. Deliberately opens
            its own connection via get_connection() rather than accepting
            one as an argument: this runs inside a ThreadPoolExecutor worker
            in parallel mode, and this codebase's connections are
            thread-local (see database.get_connection) -- a connection
            created on the calling thread isn't safe to use here. When
            accept_locs is None (e.g. a caller just wants the raw scrape
            results), both fall back to the original buffer-then-return-
            everything behavior with no DB access at all.
    """
    log.info("=" * 60)
    log.info("%s: %s", name, url)
    conn = get_connection() if accept_locs is not None else None

    # Jobright's own public minisite API -- see _scrape_jobright_minisite_api.
    # Supersedes both the Jobright-iframe CSS scraper (Intern List) and the
    # Airtable Button-field scraper (NewGrad Jobs) below: both were capped at
    # whatever the widget rendered on first view, when this pulls the real
    # full dataset directly. Configure a site for this in sites.yaml with a
    # url of "jobright-category:<category-slug>", e.g.
    # "jobright-category:intern:us:swe" or "jobright-category:newgrad:us:swe".
    if url.startswith("jobright-category:"):
        category = url[len("jobright-category:"):]
        strategy = "jobright_minisite_api"
        if conn is not None:
            on_job, flush, store_stats = _make_streaming_sink(
                conn, name, strategy, accept_locs or [], reject_locs or [])
            jobs = _scrape_jobright_minisite_api(category, on_job=on_job)
            flush()
            return {
                "name": name, "url": url,
                "status": "PASS" if jobs else "FAIL",
                "jobs": [], "already_stored": True,
                "stored_new": store_stats["new"], "stored_existing": store_stats["existing"],
                "total": len(jobs), "titles": len(jobs),
                "strategy": strategy,
            }
        jobs = _scrape_jobright_minisite_api(category)
        return {
            "name": name,
            "url": url,
            "status": "PASS" if jobs else "FAIL",
            "jobs": jobs,
            "total": len(jobs),
            "titles": len(jobs),
            "strategy": strategy,
        }

    # Airtable-embedded job boards (Button-field apply links) need the
    # bespoke expand-and-read scraper above -- the generic two-phase strategy
    # finds titles fine but can't read a Button field's href from the
    # virtualized grid view, only from each row's expanded record modal.
    # Kept as a fallback for any future site with this same structure; the
    # jobright-category path above is what NewGrad Jobs actually uses now.
    if "airtable.com/embed/" in url:
        strategy = "airtable_button_expand"
        if conn is not None:
            known_ids = {
                r["airtable_record_id"] for r in conn.execute(
                    "SELECT airtable_record_id FROM jobs "
                    "WHERE site = ? AND airtable_record_id IS NOT NULL",
                    (name,),
                ).fetchall()
            }
            on_job, flush, store_stats = _make_streaming_sink(
                conn, name, strategy, accept_locs or [], reject_locs or [])
            scrape_stats: dict = {}
            jobs = _scrape_airtable_button_grid(
                url, known_record_ids=known_ids, on_job=on_job, stats=scrape_stats)
            flush()
            # A caught-up steady-state pass legitimately finds zero NEW jobs
            # -- that's success, not failure. Only call it FAIL when the
            # scrape examined nothing at all (grid didn't load, selector
            # changed, etc), using rows_examined rather than len(jobs) to
            # tell the two apart.
            examined = scrape_stats.get("rows_examined", len(jobs))
            return {
                "name": name, "url": url,
                "status": "PASS" if (jobs or examined or known_ids) else "FAIL",
                "jobs": [], "already_stored": True,
                "stored_new": store_stats["new"], "stored_existing": store_stats["existing"],
                "total": len(jobs), "titles": len(jobs),
                "skipped_known": scrape_stats.get("skipped_known", 0),
                "strategy": strategy,
            }
        jobs = _scrape_airtable_button_grid(url)
        return {
            "name": name,
            "url": url,
            "status": "PASS" if jobs else "FAIL",
            "jobs": jobs,
            "total": len(jobs),
            "titles": len(jobs),
            "strategy": strategy,
        }

    # Step 1: Collect intelligence
    log.info("[1] Collecting page intelligence...")
    t0 = time.time()
    intel = collect_page_intelligence(url)
    collect_time = time.time() - t0
    log.info("Done in %.1fs | JSON-LD: %d | API: %d | testids: %d | cards: %d",
             collect_time, len(intel["json_ld"]), len(intel["api_responses"]),
             len(intel["data_testids"]), len(intel["card_candidates"]))

    # Headful retry if page content is tiny
    full_html = intel.get("full_html", "")
    cleaned_check = clean_page_html(full_html) if full_html else ""
    _captcha_signals = ["captcha", "are you a human", "verify you", "unusual requests",
                        "access denied", "please verify", "bot detection"]
    _is_captcha = any(s in full_html.lower() for s in _captcha_signals) if full_html else False
    if len(cleaned_check) < 5000 and full_html and not _is_captcha:
        log.info("Cleaned HTML only %s chars -- retrying headful...", f"{len(cleaned_check):,}")
        intel = collect_page_intelligence(url, headless=False)
        collect_time = time.time() - t0
        log.info("Headful done in %.1fs | JSON-LD: %d | API: %d",
                 collect_time, len(intel["json_ld"]), len(intel["api_responses"]))
    elif _is_captcha:
        log.warning("CAPTCHA/rate-limit detected -- skipping headful retry")

    # Step 1.5: Judge filters API responses
    if intel["api_responses"]:
        log.info("[1.5] Judge filtering API responses...")
        intel["api_responses"] = judge_api_responses(intel["api_responses"])
        log.info("Kept %d relevant responses", len(intel["api_responses"]))

    # Step 2: Strategy selection
    briefing = format_strategy_briefing(intel)
    log.info("[2] Phase 1: Strategy selection (%s chars briefing)", f"{len(briefing):,}")

    prompt = STRATEGY_PROMPT.format(briefing=briefing)
    try:
        raw, elapsed, meta = ask_llm(prompt)
    except Exception as e:
        log.error("LLM_ERROR: %s", e)
        return {"name": name, "status": "LLM_ERROR", "error": str(e)}

    log.info("LLM: %d chars, %.1fs", meta["response_chars"], elapsed)

    try:
        plan = extract_json(raw)
    except Exception as e:
        log.error("PARSE_ERROR: %s | raw: %s", e, raw[:500])
        return {"name": name, "status": "PARSE_ERROR", "error": str(e), "raw": raw}

    strategy = plan.get("strategy", "?")
    reasoning = plan.get("reasoning", "?")
    log.info("Strategy: %s | Reasoning: %s", strategy, reasoning)

    # Step 3: Execute
    log.info("[3] Executing %s...", strategy)
    try:
        if strategy == "json_ld":
            log.info("Extraction plan: %s", json.dumps(plan.get("extraction", {}))[:300])
            jobs = execute_json_ld(intel, plan)
        elif strategy == "api_response":
            log.info("Extraction plan: %s", json.dumps(plan.get("extraction", {}))[:300])
            jobs = execute_api_response(intel, plan)
        elif strategy == "css_selectors":
            log.info("-> Phase 2: Generating selectors from card examples...")
            selectors, jobs = execute_css_selectors(intel)
            plan["extraction"] = selectors
        else:
            log.warning("Unknown strategy: %s", strategy)
            jobs = []
    except Exception as e:
        log.error("EXECUTION_ERROR: %s", e)
        return {"name": name, "status": "EXEC_ERROR", "error": str(e), "plan": plan}

    # Step 4: Report
    titles = sum(1 for j in jobs if j.get("title"))
    total = len(jobs)
    status = "PASS" if total > 0 and titles / max(total, 1) >= 0.8 else "FAIL" if total == 0 else "PARTIAL"

    urls = sum(1 for j in jobs if j.get("url"))
    salaries = sum(1 for j in jobs if j.get("salary"))
    descs = sum(1 for j in jobs if j.get("description"))
    log.info("RESULT: %s -- %d jobs, %d titles, %d urls, %d salaries, %d descriptions",
             status, total, titles, urls, salaries, descs)

    for j in jobs[:3]:
        log.info("  - %s | loc: %s | salary: %s",
                 str(j.get("title") or "?")[:55],
                 str(j.get("location") or "?")[:25],
                 str(j.get("salary") or "-")[:20])

    return {
        "name": name,
        "status": status,
        "strategy": strategy,
        "total": total,
        "titles": titles,
        "plan": plan,
        "jobs": jobs,
        "sample": jobs[:5],
    }


# -- Target building --------------------------------------------------------

def build_scrape_targets(
    sites: list[dict] | None = None,
    search_cfg: dict | None = None,
) -> list[dict]:
    """Build the full list of (name, url) targets from sites + search config queries.

    - "search" sites get expanded: 1 URL per query from search config
    - "static" sites get scraped once as-is

    Placeholders in URLs:
      {query_encoded} -> URL-encoded search query
      {location_encoded} -> URL-encoded location
      {query} -> raw search query (for simple substitution)
    """
    if sites is None:
        sites = load_sites()
    if search_cfg is None:
        search_cfg = config.load_search_config()

    queries_cfg = search_cfg.get("queries", [])
    queries = [q["query"] for q in queries_cfg]
    locs = search_cfg.get("locations", [])
    default_location = locs[0]["location"] if locs else ""

    targets: list[dict] = []

    for site in sites:
        site_url = site.get("url", "")
        site_name = site.get("name", "Unknown")
        site_type = site.get("type", "static")

        if site_type == "search" and queries:
            for query in queries:
                expanded_url = site_url
                expanded_url = expanded_url.replace("{query_encoded}", quote_plus(query))
                expanded_url = expanded_url.replace("{query}", quote_plus(query))
                expanded_url = expanded_url.replace("{location_encoded}", quote_plus(default_location))
                targets.append({
                    "name": site_name,
                    "url": expanded_url,
                    "query": query,
                })
        else:
            expanded_url = site_url
            expanded_url = expanded_url.replace("{location_encoded}", quote_plus(default_location))
            targets.append({
                "name": site_name,
                "url": expanded_url,
                "query": None,
            })

    return targets


# -- Run all sites -----------------------------------------------------------

def _run_all(
    targets: list[dict],
    accept_locs: list[str],
    reject_locs: list[str],
    workers: int = 1,
) -> dict:
    """Run smart extract on all targets.

    Sequential by default. When workers > 1, scrapes multiple sites in parallel
    using ThreadPoolExecutor. DB storage is still serialized after each result.
    """
    conn = init_db()
    pre_stats = get_stats(conn)
    log.info("Database: %d jobs already stored, %d pending detail scrape",
             pre_stats["total"], pre_stats["pending_detail"])

    results: list[dict] = []
    total_new = 0
    total_existing = 0

    def _process_result(r: dict, target: dict) -> None:
        nonlocal total_new, total_existing
        # The Jobright-API and Airtable-grid fast paths in _run_one_site
        # already stored their own results as they were found (see
        # _make_streaming_sink) -- storing "jobs" again here would just
        # re-insert (harmlessly, since url is the PRIMARY KEY, but it would
        # double-count total_new/total_existing and pay a wasted query).
        if r.get("already_stored"):
            total_new += r.get("stored_new", 0)
            total_existing += r.get("stored_existing", 0)
            log.info("DB: +%d new, %d already existed (stored incrementally during scrape)",
                     r.get("stored_new", 0), r.get("stored_existing", 0))
            return
        jobs = r.get("jobs", [])
        if jobs:
            new, existing = _store_jobs_filtered(conn, jobs, target["name"],
                                                  r.get("strategy", "?"),
                                                  accept_locs, reject_locs)
            total_new += new
            total_existing += existing
            log.info("DB: +%d new, %d already existed", new, existing)

    if workers > 1 and len(targets) > 1:
        # Parallel mode
        with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
            future_to_target = {
                pool.submit(_run_one_site, target["name"], target["url"],
                            accept_locs, reject_locs): target
                for target in targets
            }
            for future in as_completed(future_to_target):
                target = future_to_target[future]
                r = future.result()
                results.append(r)
                _process_result(r, target)
    else:
        # Sequential mode (default)
        for i, target in enumerate(targets):
            label = target["name"]
            if target.get("query"):
                label = f"{target['name']} [{target['query']}]"
            log.info("[%d/%d] %s", i + 1, len(targets), label)

            r = _run_one_site(target["name"], target["url"], accept_locs, reject_locs)
            results.append(r)
            _process_result(r, target)

    # Summary
    for r in results:
        strategy = r.get("strategy", "?")
        if r["status"] in ("PASS", "PARTIAL", "FAIL"):
            detail = f"{r['total']} jobs, {r['titles']} titles, strategy={strategy}"
        else:
            detail = r.get("error", "")[:60]
        log.info("%-10s | %-25s | %s", r["status"], r["name"], detail)

    passed = sum(1 for r in results if r["status"] == "PASS")
    log.info("%d/%d PASS", passed, len(results))

    return {"total_new": total_new, "total_existing": total_existing,
            "passed": passed, "total": len(results)}


# -- Public entry point ------------------------------------------------------

def run_smart_extract(
    sites: list[dict] | None = None,
    workers: int = 1,
) -> dict:
    """Main entry point for AI-powered smart extraction.

    Loads sites from config/sites.yaml and search queries from the user's
    search config, then runs the extraction pipeline on all targets.

    Args:
        sites: Override the site list. If None, loads from YAML.
        workers: Number of parallel threads for site scraping. Default 1 (sequential).

    Returns:
        Dict with stats: total_new, total_existing, passed, total.
    """
    search_cfg = config.load_search_config()
    accept_locs, reject_locs = _load_location_filter(search_cfg)

    targets = build_scrape_targets(sites=sites, search_cfg=search_cfg)

    if not targets:
        log.warning("No scrape targets configured. Create config/sites.yaml and searches.yaml.")
        return {"total_new": 0, "total_existing": 0, "passed": 0, "total": 0}

    search_sites = sum(1 for s in (sites or load_sites()) if s.get("type") == "search")
    static_sites = sum(1 for s in (sites or load_sites()) if s.get("type") != "search")
    log.info("Sites: %d searchable, %d static | Total targets: %d (workers=%d)",
             search_sites, static_sites, len(targets), workers)

    return _run_all(targets, accept_locs, reject_locs, workers=workers)
