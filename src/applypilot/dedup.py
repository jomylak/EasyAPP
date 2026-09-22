"""Duplicate and related-posting detection.

Every job is stored. Nothing is deleted or skipped for being similar; the
browse view just shows one row per posting and lets you expand the rest.

Three outcomes, strictest first:

1. DUPLICATE (a repost of a job we already have) -- only when we are sure:
     - same (ats, tenant, job_id) AND the exact same title, or
     - same company AND exact title AND exact location AND identical
       description text (full description once enriched; at discovery time,
       the identical short blurb) -- unless both rows carry an ATS job id and
       the ids differ, which means two separate requisitions (another chance
       at the ATS), never one job.
   Duplicates form a cluster. The newest posting (posted_date, else
   discovered_at) is the one left visible (duplicate_of NULL); every older
   member points at it and shows in its group no matter how old.
   (Jobright issues a new jobs/info id on every repost; the ATS job id is what
   stays the same, and it only becomes known after enrichment.)

2. RELATED (the same posting across cities) -- same company, exact same title,
   a different location, and description text >= 0.95 similar, so a location
   line or two in the body doesn't break the match. All stay visible and share
   a group_id so browse can show them together.

3. Everything else is left completely alone -- including similar titles, the
   same company's different roles, and identical postings under different ATS
   ids. Those are separate applications and each stays its own row.

Deliberately no cross-company matching.

link() runs once per row at the moment it gains the data a signal needs
(insert, enrichment, scoring). Matching is symmetric and always re-elects the
newest cluster member as the visible one, so it is safe to re-run and never
produces two rows pointing at each other. There is no per-cycle full-table
pass; backfill() is only for an explicit rebuild after a rule change.
"""

import difflib
import re
import sqlite3
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot import ats as ats_module
from applypilot.config import normalize_company

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "jr_id",
}

# Below this, a description is too generic to trust as evidence of anything.
_MIN_TEXT_LEN = 30

# Same posting listed in several cities differs only by a location line or two;
# measured on live data, same-title/other-city pairs cluster at 0.95-1.00 and the
# 0.85-0.95 band is different teams or scope sharing a template.
_RELATED_DESC_SIMILARITY = 0.95

# Columns copied onto a newly-visible row from a cluster member that already
# has them, so a repost never pays for a second scrape or LLM scoring pass.
_INHERIT_ENRICHMENT = (
    "full_description", "application_url", "detail_scraped_at", "ats", "ats_job_id",
)
_INHERIT_SCORING = (
    "fit_score", "score_reasoning", "scored_at", "score_cost_usd", "company",
    "company_normalized", "company_prestige", "company_tier", "eligible",
    "eligibility_reason", "desirability_score", "keywords", "pay_text",
    "pay_below_floor", "pay_min_hourly", "pay_max_hourly", "term",
    "is_terminal_internship", "is_terminal_internship_likely", "terminal_source",
    "terminal_evidence_llm", "terminal_evidence_hint", "is_remote_spring_internship",
    "requires_returning_student", "resume_variant",
)


def canonicalize_url(url: str) -> str:
    """Strip known tracking params so campaign-tagged re-shares of the same
    link collapse to one URL before the PRIMARY KEY check ever sees them."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                        urlencode(kept), parts.fragment))


def normalize_location(location: str | None) -> str | None:
    """Collapse '' and whitespace-only strings to None so "no location known"
    compares the same regardless of which ingestion path wrote the row."""
    if location is None:
        return None
    stripped = location.strip()
    return stripped or None


def norm_title(title: str | None) -> str:
    """Case, whitespace and dash-style only. Anything more aggressive (stripping
    suffixes, req numbers, role words) risks merging genuinely different jobs."""
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[‐-―−]", "-", t)
    return " ".join(t.split())


def find_exact_text_duplicate(
    conn: sqlite3.Connection, title: str | None, text: str | None,
    location: str | None, *, text_column: str = "description",
    exclude_url: str | None = None,
) -> str | None:
    """URL of an existing row with the same title+text+location, or None.

    Only the legacy discovery paths (store_jobs, workday, jobspy) still skip
    inserts with this; the Jobright path stores everything and uses link().
    """
    if not title or not text or len(text) < _MIN_TEXT_LEN:
        return None
    query = (
        f"SELECT url FROM jobs WHERE title = ? AND {text_column} = ? "
        f"AND location IS ? AND url != ? ORDER BY discovered_at ASC LIMIT 1"
    )
    row = conn.execute(query, (title, text, location, exclude_url or "")).fetchone()
    return row["url"] if row else None


def _text_similar(a: str | None, b: str | None, threshold: float) -> bool:
    if not a or not b:
        return False
    a, b = a.lower().strip(), b.lower().strip()
    m = difflib.SequenceMatcher(None, a, b)
    # Cheap upper bounds first: the full ratio is quadratic on long text.
    return (m.real_quick_ratio() >= threshold and m.quick_ratio() >= threshold
            and m.ratio() >= threshold)


def _recency(row) -> tuple:
    return (row["posted_date"] or row["discovered_at"] or "", row["discovered_at"] or "", row["url"])


def _long(text: str | None) -> str | None:
    return text.strip() if text and len(text.strip()) >= _MIN_TEXT_LEN else None


def _texts(conn: sqlite3.Connection, url: str, cache: dict) -> tuple:
    """(full_description, blurb), each None when too short to be evidence.
    Fetched lazily: most candidates are ruled out by title alone, and loading
    every candidate's multi-KB text made a full rebuild take 20+ minutes."""
    if url not in cache:
        r = conn.execute("SELECT full_description, description FROM jobs WHERE url = ?",
                         (url,)).fetchone()
        cache[url] = (_long(r[0]), _long(r[1]))
    return cache[url]


def _ats_id(row) -> str | None:
    """Stored ATS id, or derived from the row when it hasn't been linked yet."""
    if row["ats_job_id"]:
        return row["ats_job_id"]
    return ats_module.job_key(row["ats"], row["application_url"])


def _classify(conn, me, other, cache) -> str | None:
    """'ats_job_id' | 'exact_text' (duplicate), 'related', or None."""
    if norm_title(me["title"]) != norm_title(other["title"]):
        return None
    me_ats, other_ats = _ats_id(me), _ats_id(other)
    if me_ats and me_ats == other_ats:
        return "ats_job_id"
    other_cn = other["company_normalized"] or (
        normalize_company(other["company"]) if other["company"] else None)
    if not me["company_normalized"] or me["company_normalized"] != other_cn:
        return None

    same_loc = normalize_location(me["location"]) == normalize_location(other["location"])
    diff_reqs = bool(me_ats and other_ats)  # both known (equal ids returned above)
    me_full, me_blurb = _texts(conn, me["url"], cache)
    ot_full, ot_blurb = _texts(conn, other["url"], cache)
    # Compare full text only when both sides have it, otherwise the short
    # discovery blurbs; never a full text against a blurb.
    a, b = (me_full, ot_full) if me_full and ot_full else (me_blurb, ot_blurb)
    if not a or not b:
        # A self-reported application (Gmail scan) has no text or location to
        # compare. Same company and exact title is enough to show it beside the
        # real posting, so its "you applied" record is visible there.
        return "related" if "manual" in (me["apply_backend"], other["apply_backend"]) else None
    if same_loc:
        return "exact_text" if a == b and not diff_reqs else None
    return "related" if _text_similar(a, b, _RELATED_DESC_SIMILARITY) else None


_COLS = ("url, title, location, application_url, ats, ats_job_id, company, "
         "company_normalized, discovered_at, posted_date, duplicate_of, group_id, fit_score, "
         "apply_backend")


def _copy_missing(conn: sqlite3.Connection, dest: str, donor: str, cols: tuple) -> None:
    sets = ", ".join(f"{c} = COALESCE({c}, (SELECT {c} FROM jobs WHERE url = ?))" for c in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE url = ?", (*[donor] * len(cols), dest))


def link(conn: sqlite3.Connection, url: str) -> dict:
    """Detect duplicates/related rows for one row and persist the result.

    Returns {"duplicate_of": url|None (this row's visible replacement),
             "reason": str|None, "ats_job_id": str|None, "group_id": str|None}.
    """
    me = conn.execute(f"SELECT {_COLS} FROM jobs WHERE url = ?", (url,)).fetchone()
    if me is None:
        return {"duplicate_of": None, "reason": None, "ats_job_id": None, "group_id": None}

    ats_job_id = ats_module.job_key(me["ats"], me["application_url"]) or me["ats_job_id"]
    company_norm = me["company_normalized"] or (
        normalize_company(me["company"]) if me["company"] else None) or None
    conn.execute("UPDATE jobs SET ats_job_id = ?, company_normalized = ? WHERE url = ?",
                 (ats_job_id, company_norm, url))
    me = conn.execute(f"SELECT {_COLS} FROM jobs WHERE url = ?", (url,)).fetchone()

    candidates = {}
    if ats_job_id:
        for r in conn.execute(f"SELECT {_COLS} FROM jobs WHERE ats_job_id = ? AND url != ?",
                              (ats_job_id, url)):
            candidates[r["url"]] = r
    if company_norm:
        # Rows never linked yet may have company but no company_normalized.
        for r in conn.execute(f"SELECT {_COLS} FROM jobs WHERE url != ? AND (company_normalized = ? "
                              f"OR (company_normalized IS NULL AND company IS NOT NULL))",
                              (url, company_norm)):
            candidates[r["url"]] = r

    cache: dict = {}
    dups: dict[str, str] = {}
    related: list[str] = []
    for u, other in candidates.items():
        kind = _classify(conn, me, other, cache)
        if kind == "related":
            related.append(u)
        elif kind:
            dups[u] = kind

    reason = None
    rep = url
    if dups:
        seeds = {url, *dups}
        reps = {(conn.execute("SELECT duplicate_of FROM jobs WHERE url = ?", (s,)).fetchone()
                 or {"duplicate_of": None})["duplicate_of"] or s for s in seeds}
        marks = ",".join("?" * len(reps))
        members = conn.execute(
            f"SELECT {_COLS} FROM jobs WHERE url IN ({marks}) OR duplicate_of IN ({marks})",
            (*reps, *reps)).fetchall()
        rep = max(members, key=_recency)["url"]
        reason = "ats_job_id" if "ats_job_id" in dups.values() else "exact_text"
        gid = next((m["group_id"] for m in members if m["group_id"]), None) or rep
        touched = {m["url"] for m in members}
        for m in members:
            if m["url"] == rep:
                conn.execute("UPDATE jobs SET duplicate_of = NULL, duplicate_reason = NULL, "
                             "group_id = ? WHERE url = ?", (gid, rep))
            else:
                new_reason = reason if (m["url"] in dups or m["url"] == url) else None
                conn.execute("UPDATE jobs SET duplicate_of = ?, "
                             "duplicate_reason = COALESCE(?, duplicate_reason, ?), group_id = ? "
                             "WHERE url = ?", (rep, new_reason, reason, gid, m["url"]))
        # A repost becoming the visible row must not cost a second scrape/score.
        donors = [m for m in members if m["url"] != rep]
        for cols, needs in ((_INHERIT_ENRICHMENT, "full_description"), (_INHERIT_SCORING, "fit_score")):
            have = conn.execute(f"SELECT {needs} FROM jobs WHERE url = ?", (rep,)).fetchone()[0]
            donor = max((m for m in donors if conn.execute(
                f"SELECT {needs} FROM jobs WHERE url = ?", (m["url"],)).fetchone()[0] is not None),
                key=_recency, default=None)
            if have is None and donor:
                _copy_missing(conn, rep, donor["url"], cols)
        touched_urls = touched
    else:
        touched_urls = {url}

    if related:
        gids = {conn.execute("SELECT group_id FROM jobs WHERE url = ?", (u,)).fetchone()[0]
                for u in [*related, *touched_urls]} - {None}
        target = min(gids) if gids else min([*related, *touched_urls])
        marks = ",".join("?" * len(gids)) if gids else "''"
        rel_marks = ",".join("?" * len(related))
        tch_marks = ",".join("?" * len(touched_urls))
        conn.execute(
            f"UPDATE jobs SET group_id = ? WHERE group_id IN ({marks}) "
            f"OR url IN ({rel_marks}) OR duplicate_of IN ({rel_marks}) OR url IN ({tch_marks})",
            (target, *gids, *related, *related, *touched_urls))

    conn.commit()
    row = conn.execute("SELECT duplicate_of, duplicate_reason, group_id FROM jobs WHERE url = ?",
                       (url,)).fetchone()
    return {"duplicate_of": row["duplicate_of"], "reason": row["duplicate_reason"],
            "ats_job_id": ats_job_id, "group_id": row["group_id"]}


# Kept under its old name for the callers that already run it at enrichment
# and scoring time (enrichment/detail.py, scoring/scorer.py, web/queries.py).
check_duplicate = link


def backfill(conn: sqlite3.Connection, *, batch_log_every: int = 1000) -> dict:
    """Rebuild every duplicate/group link from scratch under the current rules.

    Only for an explicit rebuild after a rule change (`applypilot
    dedup-backfill`); normal operation links each row once via link().
    """
    conn.execute("UPDATE jobs SET duplicate_of = NULL, duplicate_reason = NULL, group_id = NULL")
    conn.commit()
    urls = [r["url"] for r in conn.execute("SELECT url FROM jobs ORDER BY discovered_at ASC")]
    for i, u in enumerate(urls, 1):
        link(conn, u)
        if batch_log_every and i % batch_log_every == 0:
            print(f"  ...{i}/{len(urls)} linked")
    stats = {"processed": len(urls), "by_reason": {}}
    for r in conn.execute("SELECT duplicate_reason, COUNT(*) n FROM jobs "
                          "WHERE duplicate_of IS NOT NULL GROUP BY 1"):
        stats["by_reason"][r["duplicate_reason"]] = r["n"]
    stats["duplicates_found"] = sum(stats["by_reason"].values())
    stats["grouped_rows"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE group_id IN "
        "(SELECT group_id FROM jobs WHERE group_id IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    return stats
