"""Duplicate-posting detection.

Two checkpoints, run at two different pipeline moments because the data
needed for each isn't available any earlier:

1. **At discovery** (`canonicalize_url` + `find_exact_text_duplicate` used by
   `database.store_jobs`): before a row is even inserted. Only `title`,
   `description` (the short discovery-time blurb, not the full scraped
   text) and `location` exist yet -- `company` isn't populated until
   scoring, so it can't be part of this key. Catches re-crawls of the exact
   same listing (verified against production data: Jobright reissues a
   fresh internal job id for the same posting on repeat crawls -- one real
   posting showed up as 39 different URLs this way).

2. **Right after enrichment** (`check_duplicate`, called once
   `application_url`/`ats`/`full_description` are written): two
   independent signals, either one enough to confirm a duplicate --
     a. Exact match on (title, full_description, location) -- same idea as
        checkpoint 1 but with the much richer post-enrichment text, so it
        catches cases the short discovery blurb couldn't tell apart.
     b. Same (ats, tenant, job_id) per ats.extract_job_id, AND a
        similar-enough title. The title check matters: verified against
        production data that the same Greenhouse (tenant, token) pair can
        point at two genuinely different postings at two different
        companies (a Jobright data bug, not a dedup false positive) --
        titles diverging is what catches it. tenant scopes the match
        instead of the `company` column for the same reason as above
        (company isn't populated yet, and even once it is, the same
        employer appears under several spellings that would need fuzzy
        matching to unify -- see config.normalize_company's docstring).

Deliberately NOT covered here: two postings with identical title+description
but a genuinely *different* location and no resolvable ATS id. That's the
ambiguous case (a real per-office requisition vs. a re-crawl artifact can't
be told apart from text alone) -- it's meant for the review tab, not an
auto-decision either way.

3. **After scoring** (`find_company_duplicate`, re-run from
   scoring/scorer.py's per-row loop once `company` is known): the same
   employer's posting can show up through more than one ATS tenant, or
   through no resolvable ATS at all, so find_ats_duplicate's tenant-scoped
   match never sees it. This closes that gap with the same conservative
   philosophy as everything else here: normalized company AND similar
   title AND similar full_description AND an EXACT location match are all
   required -- location is deliberately not allowed to be fuzzy, since a
   genuinely different office for the same role is a different req, not a
   repost, and that's the one signal text similarity can't safely stand in
   for. (A "same ATS job_id, different tenant" signal was considered and
   rejected: tenant is derived from the ATS URL structure itself, so a
   different tenant for the same employer essentially never happens --
   in practice a different tenant means a different employer, which is
   exactly the false-positive case find_ats_duplicate's own title check
   guards against.)

4. **Also after scoring** (`find_cross_company_duplicate`, same call site as
   #3, run only if #3 found nothing): catches a near-verbatim repost under a
   company name that doesn't even match -- a staffing mill spamming the same
   template across many legally-distinct subsidiary brands, or a
   parent/subsidiary pair (TikTok/ByteDance) posting the identical req under
   both names. Company is dropped from the key entirely (it's the field
   this exists to route around), so the bar on title and description
   similarity is raised well above #3's to compensate -- near-verbatim text
   is the only signal left standing between a real duplicate and two
   different postings that happen to share boilerplate. Location still
   must match exactly, same reasoning as #3.
"""

import difflib
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot import ats as ats_module
from applypilot.config import normalize_company

# Query params that are pure tracking noise -- stripping them collapses
# "the same URL, different campaign" without touching params some ATS
# platforms use as the actual job identifier (e.g. Greenhouse's `token=`,
# IBM's `jobId=`), which must never be stripped.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "jr_id",
}

# Below this, a discovery-time blurb is too generic ("Apply now!", teaser
# boilerplate) to trust as a duplicate signal even on an exact match.
_MIN_TEXT_LEN = 30

# difflib ratio above which two titles are "the same role, formatting
# noise" (whitespace/dash/unicode differences, a trailing "- City, ST").
# Calibrated against production data: genuine same-posting title pairs
# scored 0.78-0.95, genuine different-posting pairs scored 0.26-0.67.
_TITLE_SIMILARITY_THRESHOLD = 0.7

# Stricter than the title threshold: full_description text is long and
# specific enough that a coincidental 0.85+ match is vanishingly unlikely
# to be a different posting, whereas two genuinely different titles for
# the same underlying role can still legitimately fall under 0.85 on
# description text alone -- title and description each need their own bar.
_DESC_SIMILARITY_THRESHOLD = 0.85

# find_cross_company_duplicate's bar, well above the company-scoped one
# above: without company or exact title as a supporting signal, this is the
# only thing standing between "same posting, different label" and "two
# different jobs that happen to use similar boilerplate." Calibrated to
# require near-verbatim text -- a templated staffing-mill repost or a
# parent/subsidiary pair (TikTok/ByteDance) posting the identical req under
# two different brand names, not merely a similar-sounding role.
_CROSS_COMPANY_TITLE_SIMILARITY_THRESHOLD = 0.85
_CROSS_COMPANY_DESC_SIMILARITY_THRESHOLD = 0.93


def canonicalize_url(url: str) -> str:
    """Strip known tracking params so campaign-tagged re-shares of the same
    link collapse to one URL before the PRIMARY KEY check ever sees them."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                        urlencode(kept), parts.fragment))


def normalize_location(location: str | None) -> str | None:
    """Collapse '' and whitespace-only strings to None, so "no location
    known" is stored the same way regardless of which ingestion path wrote
    the row. Without this, dedup's `location IS ?` comparisons (NULL-safe,
    but not ""-safe) silently fail to match a NULL-location row from one
    source against an ""-location row from another for the same posting --
    verified against production data: workday.py defaulted a missing
    location to "" while jobspy.py defaulted the same case to None."""
    if location is None:
        return None
    stripped = location.strip()
    return stripped or None


def _text_similar(a: str | None, b: str | None, threshold: float) -> bool:
    if not a or not b:
        return False
    ratio = difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()
    return ratio >= threshold


def _titles_similar(a: str | None, b: str | None) -> bool:
    return _text_similar(a, b, _TITLE_SIMILARITY_THRESHOLD)


def find_exact_text_duplicate(
    conn: sqlite3.Connection, title: str | None, text: str | None,
    location: str | None, *, text_column: str = "description",
    exclude_url: str | None = None,
) -> str | None:
    """URL of an existing row with the same title+text+location, or None.

    `text_column` picks which column to compare against ("description" at
    discovery time, "full_description" post-enrichment).
    """
    if not title or not text or len(text) < _MIN_TEXT_LEN:
        return None
    query = (
        f"SELECT url FROM jobs WHERE title = ? AND {text_column} = ? "
        f"AND location IS ? AND url != ? ORDER BY discovered_at ASC LIMIT 1"
    )
    row = conn.execute(query, (title, text, location, exclude_url or "")).fetchone()
    return row["url"] if row else None


def find_ats_duplicate(
    conn: sqlite3.Connection, ats: str | None, application_url: str | None,
    title: str | None, *, exclude_url: str | None = None,
) -> tuple[str | None, str | None]:
    """(canonical_url, ats_job_id) for a same-(ats,tenant,job_id) row whose
    title is close enough to trust, or (None, ats_job_id) if no match.

    ats_job_id is returned even on no-match so the caller can still persist
    it on this row for future comparisons.
    """
    resolved = ats_module.extract_job_id(ats, application_url)
    if not resolved:
        return None, None
    tenant, job_id = resolved
    ats_job_id = f"{ats}:{tenant}:{job_id}"

    rows = conn.execute(
        "SELECT url, title FROM jobs WHERE ats_job_id = ? AND url != ? "
        "ORDER BY discovered_at ASC",
        (ats_job_id, exclude_url or ""),
    ).fetchall()
    for row in rows:
        if _titles_similar(title, row["title"]):
            return row["url"], ats_job_id
    return None, ats_job_id


def find_company_duplicate(
    conn: sqlite3.Connection, company: str | None, title: str | None,
    text: str | None, location: str | None, *,
    text_column: str = "full_description", exclude_url: str | None = None,
) -> str | None:
    """URL of an existing row at the same normalized company whose title,
    description, and location are all close enough to trust as the same
    req reposted/relisted under a different ATS tenant (or no ATS at all)
    -- the case find_ats_duplicate can't catch because it scopes its match
    to one (ats, tenant) pair.

    All three of company + title + description must agree, and location
    must match exactly (same NULL-safe comparison as
    find_exact_text_duplicate). Location is deliberately not fuzzy: per
    this module's docstring, two postings at the same company with a
    genuinely different location are a different req, not a repost, and
    that's the one signal text similarity can't safely stand in for.
    """
    normalized = normalize_company(company) if company else None
    if not normalized or not title or not text or len(text) < _MIN_TEXT_LEN:
        return None
    rows = conn.execute(
        f"SELECT url, title, {text_column} AS text FROM jobs "
        f"WHERE company_normalized = ? AND url != ? AND location IS ? "
        f"ORDER BY discovered_at ASC",
        (normalized, exclude_url or "", location),
    ).fetchall()
    for row in rows:
        if (_text_similar(title, row["title"], _TITLE_SIMILARITY_THRESHOLD)
                and _text_similar(text, row["text"], _DESC_SIMILARITY_THRESHOLD)):
            return row["url"]
    return None


def find_cross_company_duplicate(
    conn: sqlite3.Connection, title: str | None, text: str | None,
    location: str | None, *, text_column: str = "full_description",
    exclude_url: str | None = None,
) -> str | None:
    """URL of an existing row -- possibly under a completely different
    company name -- whose title and description both match at a much
    higher bar than find_company_duplicate requires.

    For the pattern find_company_duplicate can't catch because it insists
    on company agreeing first: a near-verbatim template reposted under a
    different company string entirely. Two real-world sources of this,
    both confirmed in production data:
      - Staffing-mill spam (e.g. Arthur J. Gallagher's many legally-distinct
        subsidiary brands each posting the identical "AI Developer" template).
      - A parent/subsidiary pair posting the same req under both brand names
        (TikTok/ByteDance).
    Company is deliberately not part of the key here -- it's the one field
    this function exists to route around -- but location still must match
    exactly, same NULL-safe comparison and same reasoning as everywhere else
    in this module: a genuinely different office is a different req no
    matter how similar the text.

    Both thresholds sit well above the company-scoped ones: without company
    or exact title as a supporting signal, near-verbatim text is the only
    thing separating a real duplicate from two different postings that
    happen to share boilerplate.
    """
    if not title or not text or len(text) < _MIN_TEXT_LEN:
        return None
    rows = conn.execute(
        f"SELECT url, title, {text_column} AS text FROM jobs "
        f"WHERE location IS ? AND url != ? AND {text_column} IS NOT NULL "
        f"ORDER BY discovered_at ASC",
        (location, exclude_url or ""),
    ).fetchall()
    for row in rows:
        row_text = row["text"]
        # Cheap length pre-filter before the O(n*m) difflib comparison below:
        # two postings within the description threshold of each other can't
        # differ in length by much more than the allowed edit distance.
        if row_text and abs(len(text) - len(row_text)) > 0.15 * max(len(text), len(row_text)):
            continue
        if (_text_similar(title, row["title"], _CROSS_COMPANY_TITLE_SIMILARITY_THRESHOLD)
                and _text_similar(text, row_text, _CROSS_COMPANY_DESC_SIMILARITY_THRESHOLD)):
            return row["url"]
    return None


def check_duplicate(conn: sqlite3.Connection, url: str) -> dict:
    """Run checkpoint 2 for one already-enriched row and persist the result.

    Call once `application_url`, `ats`, and `full_description` are written
    (see enrichment/detail.py). Idempotent -- safe to re-run (e.g. from the
    backfill) since it always re-derives from current column values.

    Returns {"duplicate_of": url|None, "reason": str|None, "ats_job_id": str|None}.
    """
    row = conn.execute(
        "SELECT title, full_description, location, application_url, ats, company "
        "FROM jobs WHERE url = ?", (url,),
    ).fetchone()
    if row is None:
        return {"duplicate_of": None, "reason": None, "ats_job_id": None}

    canonical, ats_job_id = find_ats_duplicate(
        conn, row["ats"], row["application_url"], row["title"], exclude_url=url,
    )
    reason = "ats_job_id" if canonical else None

    if not canonical:
        canonical = find_exact_text_duplicate(
            conn, row["title"], row["full_description"], row["location"],
            text_column="full_description", exclude_url=url,
        )
        if canonical:
            reason = "exact_text"

    if not canonical:
        canonical = find_company_duplicate(
            conn, row["company"], row["title"], row["full_description"], row["location"],
            text_column="full_description", exclude_url=url,
        )
        if canonical:
            reason = "company_repost"

    if not canonical:
        canonical = find_cross_company_duplicate(
            conn, row["title"], row["full_description"], row["location"],
            text_column="full_description", exclude_url=url,
        )
        if canonical:
            reason = "cross_company_text"

    conn.execute(
        "UPDATE jobs SET ats_job_id = ?, duplicate_of = ?, duplicate_reason = ? WHERE url = ?",
        (ats_job_id, canonical, reason, url),
    )
    conn.commit()
    return {"duplicate_of": canonical, "reason": reason, "ats_job_id": ats_job_id}


def backfill(conn: sqlite3.Connection, *, batch_log_every: int = 500) -> dict:
    """Run checkpoint 2 retroactively over every already-enriched row.

    For a database that accumulated duplicates before this module existed.
    Processes rows oldest-discovered-first so earlier rows stay canonical
    (an already-applied-to row should never end up pointing at a
    still-pending one).
    """
    urls = [r["url"] for r in conn.execute(
        "SELECT url FROM jobs WHERE full_description IS NOT NULL "
        "ORDER BY discovered_at ASC"
    ).fetchall()]

    stats = {"processed": 0, "duplicates_found": 0, "by_reason": {}}
    for i, url in enumerate(urls, 1):
        result = check_duplicate(conn, url)
        stats["processed"] += 1
        if result["duplicate_of"]:
            stats["duplicates_found"] += 1
            stats["by_reason"][result["reason"]] = stats["by_reason"].get(result["reason"], 0) + 1
        if batch_log_every and i % batch_log_every == 0:
            print(f"  ...{i}/{len(urls)} processed, {stats['duplicates_found']} duplicates found so far")
    return stats
