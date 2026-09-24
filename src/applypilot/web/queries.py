"""Read queries for the web UI.

Kept apart from server.py so the SQL can be tested without standing up an
HTTP app, and so the shape of what the browse table needs stays legible in
one place.

Two conventions matter here:

- The day expression is written exactly as `database._DAY_EXPR` spells it.
  SQLite matches an expression index by comparing the expression text, so a
  cosmetic difference silently turns every day query into a full scan.
- `keywords` is read from its own column. `score_reasoning` happens to start
  with a copy of it (scorer.py writes `keywords + "\\n" + reasoning`), and
  view.py parsed it back out of there; doing that again would re-import a bug
  rather than a feature.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot import company_limits
from applypilot.database import _DAY_EXPR, _LOCAL_TZ, get_connection

# What the browse table shows per row. Deliberately excludes full_description:
# a day of 400 rows would carry megabytes of prose nobody has expanded yet.
# view.py inlined every description into one page and produced a 21 MB file.
# How many earlier postings of this exact job were merged into it (see
# dedup.link) -- distinct (employer job id, or a Jobright id if no employer id
# was resolved) + date combos, so the same repost re-scraped twice doesn't
# double count. Shared between _ROW_COLUMNS (the "seen before" chip) and
# _filter_clauses' max_reposts filter so the two can never disagree about
# what a repost count means.
_DUP_COUNT_EXPR = """
    SELECT COUNT(DISTINCT COALESCE(NULLIF(d.ats_job_id, ''), CASE WHEN instr(d.url, 'info/') > 0 THEN substr(d.url, instr(d.url, 'info/') + 5, 24) ELSE d.url END) || '|' || date(COALESCE(d.posted_date, d.discovered_at))) FROM jobs d WHERE d.duplicate_of = jobs.url
       AND COALESCE(NULLIF(d.ats_job_id, ''), CASE WHEN instr(d.url, 'info/') > 0 THEN substr(d.url, instr(d.url, 'info/') + 5, 24) ELSE d.url END) || '|' || date(COALESCE(d.posted_date, d.discovered_at)) != COALESCE(NULLIF(jobs.ats_job_id, ''), CASE WHEN instr(jobs.url, 'info/') > 0 THEN substr(jobs.url, instr(jobs.url, 'info/') + 5, 24) ELSE jobs.url END) || '|' || date(COALESCE(jobs.posted_date, jobs.discovered_at))
"""

_ROW_COLUMNS = f"""
    url, title, company, site, location, salary, pay_text,
    fit_score, desirability_score, company_prestige, company_tier,
    job_type, ats, eligible, keywords, term,
    is_terminal_internship, is_terminal_internship_likely, terminal_evidence_hint,
    is_remote_spring_internship,
    pay_min_hourly, pay_max_hourly, pay_below_floor,
    apply_status, applied_at, apply_error, apply_cost_usd,
    post_apply_status, post_apply_status_at, post_apply_evidence, post_apply_event_date,
    queue_batch, queue_position, tailored_resume_path,
    {_DAY_EXPR} AS day,
    COALESCE(posted_date, discovered_at) AS posted,
    ({_DUP_COUNT_EXPR}) AS dup_count,
    (SELECT COUNT(DISTINCT COALESCE(NULLIF(g.ats_job_id, ''), CASE WHEN instr(g.url, 'info/') > 0 THEN substr(g.url, instr(g.url, 'info/') + 5, 24) ELSE g.url END) || '|' || date(COALESCE(g.posted_date, g.discovered_at))) FROM jobs g WHERE jobs.group_id IS NOT NULL
       AND g.group_id = jobs.group_id AND g.duplicate_of IS NULL AND g.url != jobs.url
       AND COALESCE(NULLIF(g.ats_job_id, ''), CASE WHEN instr(g.url, 'info/') > 0 THEN substr(g.url, instr(g.url, 'info/') + 5, 24) ELSE g.url END) || '|' || date(COALESCE(g.posted_date, g.discovered_at)) != COALESCE(NULLIF(jobs.ats_job_id, ''), CASE WHEN instr(jobs.url, 'info/') > 0 THEN substr(jobs.url, instr(jobs.url, 'info/') + 5, 24) ELSE jobs.url END) || '|' || date(COALESCE(jobs.posted_date, jobs.discovered_at))) AS location_count,
    (EXISTS(SELECT 1 FROM jobs d WHERE d.duplicate_of = jobs.url
            AND d.apply_status IN ('applied', 'manual', 'in_progress'))
     OR EXISTS(SELECT 1 FROM jobs g WHERE g.group_id = jobs.group_id AND g.url != jobs.url
               AND g.title = jobs.title
               AND g.apply_status IN ('applied', 'manual', 'in_progress'))) AS applied_earlier
"""

# Sortable columns, mapped to SQL. A whitelist rather than interpolation --
# the sort key arrives from a query string.
# Big tech first, then desirability, with fit demoted to a pure tiebreaker.
# "top" is the tier-boosted preset sort (used by the Big Tech/prestige/fit
# chips), not a clickable column header, so it has no user-facing direction.
_TIER_RANK = ("CASE company_tier WHEN 'tier1' THEN 2 "
              "WHEN 'adjacent' THEN 1 ELSE 0 END DESC")
_TOP_ORDER = f"{_TIER_RANK}, desirability_score DESC, fit_score DESC"

# Each clickable column: its own SQL expression, a fixed tiebreaker (applied
# after the user's chosen direction, always in its own preferred direction so
# ties don't reshuffle when the primary column flips), and the direction it
# defaults to the first time a column is clicked.
_SORT_COLUMNS: dict[str, dict] = {
    "prestige": {"expr": "company_prestige", "tiebreak": "fit_score DESC", "default_dir": "desc"},
    "fit": {"expr": "fit_score", "tiebreak": "desirability_score DESC", "default_dir": "desc"},
    "desirability": {"expr": "desirability_score", "tiebreak": "fit_score DESC", "default_dir": "desc"},
    "company": {"expr": "company COLLATE NOCASE", "tiebreak": None, "default_dir": "asc"},
    "title": {"expr": "title COLLATE NOCASE", "tiebreak": None, "default_dir": "asc"},
    "location": {"expr": "location COLLATE NOCASE", "tiebreak": None, "default_dir": "asc", "nulls": "LAST"},
    "posted": {"expr": "COALESCE(posted_date, discovered_at)", "tiebreak": None, "default_dir": "desc"},
    # Never sort on the `salary` text: it compares "$9" against "$110500"
    # lexically and puts the nine first. NULLS LAST regardless of direction --
    # an unstated pay should never sort to the top just because the user
    # flipped to ascending.
    "pay": {"expr": "pay_max_hourly", "tiebreak": None, "default_dir": "desc", "nulls": "LAST"},
}

# Kept for callers (e.g. facets()) that just want the set of valid sort keys.
SORTS: dict[str, str] = {"top": _TOP_ORDER, **{k: v["expr"] for k, v in _SORT_COLUMNS.items()}}
DEFAULT_SORT = "posted"
DEFAULT_DIR = "desc"


def _order_clause(sort: str, direction: str | None) -> tuple[str, str]:
    """SQL ORDER BY expression plus the (possibly defaulted) direction used."""
    if sort not in _SORT_COLUMNS:
        return _TOP_ORDER, "desc"
    col = _SORT_COLUMNS[sort]
    dir_ = direction if direction in ("asc", "desc") else col["default_dir"]
    order = f"{col['expr']} {'ASC' if dir_ == 'asc' else 'DESC'}"
    if col.get("nulls"):
        order += f" NULLS {col['nulls']}"
    if col.get("tiebreak"):
        order += f", {col['tiebreak']}"
    return order, dir_


def _filter_clauses(f: dict) -> tuple[str, list]:
    """Translate the UI's filter dict into SQL. Unknown keys are ignored."""
    clauses, params = [], []

    if f.get("day"):
        clauses.append(f"{_DAY_EXPR} = ?")
        params.append(f["day"])
    # Quality bars. With include_tier set, a tier1/adjacent posting is exempt
    # from every one of them -- the whole point of the tier is that a big-tech
    # opening gets applied to regardless of how it scores, and these bars are
    # exactly what used to hide them (Meta's prestige-10 internships average a
    # fit of 3.4, well under the priority panels' own floor).
    exempt = bool(f.get("include_tier"))

    def _bar(clause: str, value) -> None:
        clauses.append(f"(company_tier IS NOT NULL OR ({clause}))" if exempt else clause)
        params.append(value)

    if f.get("min_fit") is not None:
        _bar("fit_score >= ?", f["min_fit"])
    if f.get("min_desirability") is not None:
        _bar("desirability_score >= ?", f["min_desirability"])
    if f.get("min_prestige") is not None:
        _bar("company_prestige >= ?", f["min_prestige"])
    if f.get("min_pay") is not None:
        # Compared against the *high* end: "pay >= 40" asks which jobs could
        # pay at least that, and a $30-$60 posting qualifies. -1 is the
        # "unparseable" marker and NULL is "no stated pay"; both are excluded,
        # because a threshold the user typed is a deliberate act and silently
        # including unknowns would defeat it.
        _bar("pay_max_hourly IS NOT NULL AND pay_max_hourly >= ?", f["min_pay"])
    if f.get("job_type"):
        clauses.append("job_type = ?")
        params.append(f["job_type"])
    if f.get("site"):
        clauses.append("site = ?")
        params.append(f["site"])
    if f.get("ats"):
        # A checklist, not a single choice -- f["ats"] is a list (even a
        # one-item one), so this is always an IN, never a bare `=`.
        ats_list = f["ats"]
        clauses.append(f"ats IN ({','.join('?' * len(ats_list))})")
        params.extend(ats_list)
    # Unconditional, not opt-in: there is no reason this browse table should
    # ever surface a job you can't honestly take. NULL passes because a job
    # scored before the eligibility gate existed is not known to be
    # ineligible, and dropping it would hide a real opening.
    clauses.append("(eligible IS NULL OR eligible != 'no')")
    # Unconditional, not opt-in, and season-based rather than title-text
    # matching: internships only in Spring or Summer term (your only two
    # workable terms -- Spring because you're still enrolled, Summer because
    # it's right after graduation). `term` is the LLM's own read (TERM
    # CHECK); NULL/'unclear' rows (not yet re-scored, or the posting truly
    # doesn't say) fall back to the same title-text heuristic this used to
    # be gated behind, so nothing regresses ahead of a re-score. That old
    # heuristic used to require the title also say "summer" even for a
    # legitimate Spring-only posting -- fixed here, since Spring alone is
    # now explicitly wanted, not just tolerated.
    clauses.append("""(
        job_type != 'internship'
        OR term IN ('spring', 'summer')
        OR (
            (term IS NULL OR term = 'unclear')
            AND (
                (LOWER(title) NOT LIKE '%winter%' AND LOWER(title) NOT LIKE '%fall%')
                OR LOWER(title) LIKE '%summer%'
            )
        )
    )""")
    if f.get("tier_only"):
        clauses.append("company_tier IS NOT NULL")
    if f.get("location"):
        # Coarse buckets, matching _location_desirability's own tiers so the
        # filter and the score can't disagree about what "NYC" means.
        bucket = f["location"]
        if bucket == "nyc":
            clauses.append("(LOWER(location) LIKE '%new york%' "
                           "OR LOWER(location) LIKE '%, ny%' "
                           "OR LOWER(location) LIKE '%brooklyn%' "
                           "OR LOWER(location) LIKE '%queens%')")
        elif bucket == "remote":
            clauses.append("(LOWER(location) LIKE '%remote%' "
                           "OR LOWER(location) LIKE '%anywhere%')")
        elif bucket == "metro":
            metro_likes = " OR ".join(
                ["LOWER(location) LIKE '%new york%'"]
                + [f"LOWER(location) LIKE '%{m}%'" for m in
                   ("san francisco", "seattle", "austin", "boston", "palo alto",
                    "san jose", "mountain view", "sunnyvale")]
            )
            clauses.append(f"({metro_likes})")
    if f.get("term"):
        clauses.append("term = ?")
        params.append(f["term"])
    if f.get("above_pay_floor"):
        clauses.append("(pay_below_floor IS NULL OR pay_below_floor != 'yes')")
    if f.get("terminal_only"):
        # Meaningless for a non-internship row -- "will this convert to
        # full-time" only applies to internships, so a new-grad posting
        # (which is already full-time) shouldn't be hidden by a pill that's
        # asking a question that doesn't apply to it.
        clauses.append("(job_type != 'internship' OR is_terminal_internship = 'yes')")
    if f.get("likely_terminal_only"):
        # Manual-review queue: strong matches whose posting never says
        # either way about post-grad eligibility (see
        # compute_likely_terminal_internships in scoring/scorer.py). Same
        # non-internship exemption as terminal_only above.
        clauses.append(
            "(job_type != 'internship' OR is_terminal_internship_likely = 'yes')"
        )
    if f.get("eligible_only"):
        # "The only internships I can actually apply to" -- confirmed OR
        # likely terminal, OR'd rather than the AND the two pills above give
        # when combined (which is always empty: a row is never both). Kept
        # as its own filter key instead of asking the UI to check both pills
        # at once, since "either kind" and "only the confirmed kind" are
        # different questions a viewer might actually want answered.
        # Only internships need this post-grad-eligibility gate -- new grad
        # (and other non-internship) roles aren't terminal-internship-scored
        # at all, so requiring the terminal flag on them would wrongly hide
        # every new-grad posting from someone eligible for new grad roles.
        clauses.append(
            "(job_type != 'internship' "
            "OR is_terminal_internship = 'yes' OR is_terminal_internship_likely = 'yes')"
        )
    # Unconditional, not opt-in: once a job is queued/in-flight/applied it
    # must disappear from Browse everywhere, not just behind a pill the user
    # has to remember to turn on -- otherwise the same row stays clickable
    # and re-selectable while a batch is running or after it's done, which is
    # how a job ends up queued twice. The Applications tab (queries.py's
    # separate `applications()` query) is the only place these statuses are
    # meant to be visible.
    clauses.append("(apply_status IS NULL OR apply_status = 'failed')")
    # Manual override from the "Report ineligible" button -- unconditional
    # like the statuses above, not a pill, since the whole point is "stop
    # showing me this and everything dedup ties to it" (dedup.link carries
    # this onto future reposts of the same job, see its sticky-override step).
    clauses.append("reported_ineligible_at IS NULL")
    # Same reasoning for confirmed duplicates: fit_gate_sql() already excludes
    # them from the ranked apply queue and from scoring/tailoring (see its
    # docstring), so Browse must exclude them unconditionally too -- otherwise
    # a duplicate posting can be selected and queued/applied to separately
    # from its canonical row, even though nothing downstream re-checks
    # duplicate_of once a human, rather than the ranker, picked the job.
    clauses.append("duplicate_of IS NULL")
    # "How many times has this survived posting been reposted" -- a narrowing
    # filter on the canonical row itself (max_reposts=0 means "only jobs that
    # have never been reposted"), not a way to unhide any individual repost
    # row (those stay unconditionally hidden per the clause above; the "seen
    # before" chip and JobExpansion's "Related postings" panel are still the
    # only way to inspect them). None (the default) applies no filter.
    if f.get("max_reposts") is not None:
        clauses.append(f"({_DUP_COUNT_EXPR}) <= ?")
        params.append(int(f["max_reposts"]))
    # A job with no scraped description can never be scored (scoring requires
    # full_description IS NOT NULL -- see scorer.py's pending_score gate) and
    # has nothing worth reviewing yet. Whether it's still queued for enrichment
    # (detail_scraped_at IS NULL) or permanently failed after 3 attempts
    # (detail_scraped_at set, detail_error set, full_description never
    # filled), Browse should show neither -- both read as noise, not a real
    # posting. `applypilot status`'s Pending enrichment / Enrichment errors
    # counters are the right place to monitor these, not this table.
    clauses.append("full_description IS NOT NULL")
    if f.get("posted_within_days") is not None:
        # Compared against a literal Eastern-time cutoff date, not SQL's own
        # date('now', ?) -- that's a UTC calendar date and would disagree
        # with _DAY_EXPR (local_date(...), Eastern) by up to a day right
        # around the UTC/Eastern midnight gap. Still hits idx_jobs_day: the
        # index is built on the exact _DAY_EXPR text, and comparing that
        # column to a bound parameter is exactly the range scan it's for.
        cutoff = (datetime.now(_LOCAL_TZ) - timedelta(days=int(f["posted_within_days"]))).date().isoformat()
        clauses.append(f"{_DAY_EXPR} >= ?")
        params.append(cutoff)
    if f.get("q"):
        clauses.append("(title LIKE ? OR company LIKE ? OR keywords LIKE ?)")
        like = f"%{f['q']}%"
        params += [like, like, like]

    return (" AND ".join(clauses) if clauses else "1"), params


def list_days(conn: sqlite3.Connection | None = None,
              filters: dict | None = None) -> list[dict]:
    """Every day that has postings, newest first, with the chip counts.

    The counts come back with the days rather than from three more round
    trips, because the chips are labelled before anyone clicks them.

    Gated by the same eligibility/term clause list_jobs() applies unconditionally
    -- otherwise a day's total would count rows the table itself never shows,
    which reads as "10 jobs vanished" the moment you open that day.
    """
    conn = conn or get_connection()
    visible_where, params = _filter_clauses(filters or {})
    rows = conn.execute(f"""
        SELECT {_DAY_EXPR} AS day,
               COUNT(*) AS total,
               SUM(CASE WHEN company_prestige >= 9 THEN 1 ELSE 0 END) AS prestige,
               SUM(CASE WHEN desirability_score >= 6 AND fit_score >= 8
                        THEN 1 ELSE 0 END) AS best_fit,
               SUM(CASE WHEN apply_status = 'applied' THEN 1 ELSE 0 END) AS applied
        FROM jobs
        WHERE {_DAY_EXPR} IS NOT NULL AND ({visible_where})
        GROUP BY day
        ORDER BY day DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


def list_jobs(filters: dict, sort: str = DEFAULT_SORT, direction: str | None = None,
              page: int = 0, page_size: int = 30,
              conn: sqlite3.Connection | None = None) -> dict:
    """One page of one day's table."""
    conn = conn or get_connection()
    where, params = _filter_clauses(filters)
    order, dir_used = _order_clause(sort, direction)
    page_size = max(1, min(page_size, 500))

    total = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {where}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""SELECT {_ROW_COLUMNS} FROM jobs WHERE {where}
            ORDER BY {order}, url
            LIMIT ? OFFSET ?""",
        params + [page_size, page * page_size],
    ).fetchall()

    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": sort if sort in SORTS else DEFAULT_SORT,
        "dir": dir_used,
    }


def job_group(url: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every other posting dedup.link tied to this one: hidden duplicates
    (older postings/reissues of the same job) and visible related postings
    (same company, similar or same title). Newest first."""
    conn = conn or get_connection()
    row = conn.execute("SELECT group_id, duplicate_of FROM jobs WHERE url = ?", (url,)).fetchone()
    if row is None:
        return []
    rep = row["duplicate_of"] or url
    rows = conn.execute(
        f"""SELECT url, title, company, location, site, apply_status, fit_score,
                   duplicate_of, duplicate_reason, ats_job_id, {_DAY_EXPR} AS day,
                   COALESCE(posted_date, discovered_at) AS posted
            FROM jobs
            WHERE url != ? AND (url = ? OR duplicate_of = ?
                  OR (group_id IS NOT NULL AND group_id = ?))
            ORDER BY posted DESC""",
        (url, rep, rep, row["group_id"]),
    ).fetchall()
    def key(r) -> str:
        # One entry per job per day. Jobright reissues an id, and two boards
        # can carry the same listing, but they are one sighting of one job:
        # identity is the employer's own id when we have it, else the Jobright
        # id in the URL.
        u = r["url"]
        ident = r["ats_job_id"] or (u.split("info/")[1][:24] if "info/" in u else u)
        return f"{ident}|{(r['posted'] or '')[:10]}"

    me = conn.execute("SELECT url, ats_job_id, COALESCE(posted_date, discovered_at) AS posted "
                      "FROM jobs WHERE url = ?", (url,)).fetchone()
    own = key(me)
    seen: dict[str, dict] = {}
    for r in rows:
        if key(r) == own:
            continue
        d = dict(r)
        d["relation"] = ("applied" if r["apply_status"] in ("applied", "manual", "in_progress")
                         else "duplicate" if (r["duplicate_of"] or r["url"] == rep) else "related")
        prev = seen.get(key(r))
        if prev is None or (d["relation"] == "applied" and prev["relation"] != "applied"):
            seen[key(r)] = d
    return list(seen.values())


def job_detail(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Everything the expanded row shows, including the description."""
    conn = conn or get_connection()
    row = conn.execute(
        f"SELECT {_ROW_COLUMNS}, full_description, description, application_url,"
        f" score_reasoning, resume_variant, review_status, apply_attempts,"
        f" is_terminal_internship, requires_returning_student, eligibility_reason,"
        f" apply_backend, apply_duration_ms, last_attempted_at,"
        f" apply_llm_requests, apply_input_tokens, apply_output_tokens,"
        f" apply_cache_read_tokens"
        f" FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if not row:
        return None

    job = dict(row)

    # score_reasoning is stored as "keywords\nreasoning". The keywords half is
    # already its own column, so strip the duplicate first line rather than
    # showing it twice.
    reasoning = job.get("score_reasoning") or ""
    if job.get("keywords") and reasoning.startswith(job["keywords"]):
        reasoning = reasoning[len(job["keywords"]):]
    job["reasoning"] = reasoning.strip()

    # Why this resume was chosen, straight from the router that chose it.
    try:
        from applypilot.scoring.router import explain_route
        job["resume_route"] = explain_route(job)
    except Exception:
        job["resume_route"] = None

    job["company_limit"] = company_limits.status_for(conn, job.get("company")) if job.get("company") else None

    return job


def company_limit_breakdown(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Per-company cap status for every employer already applied to, for the
    Dashboard tab's breakdown."""
    conn = conn or get_connection()
    return company_limits.all_statuses(conn)


def facets(conn: sqlite3.Connection | None = None) -> dict:
    """Distinct values for the filter dropdowns."""
    conn = conn or get_connection()

    def distinct(col):
        return [r[0] for r in conn.execute(
            f"SELECT DISTINCT {col} FROM jobs "
            f"WHERE {col} IS NOT NULL AND {col} != '' ORDER BY {col}"
        ).fetchall()]

    return {
        "sites": distinct("site"),
        "job_types": distinct("job_type"),
        "ats": distinct("ats"),
        "sorts": list(SORTS),
    }


def stats(conn: sqlite3.Connection | None = None) -> dict:
    """Dashboard tiles.

    One pass over the table with conditional sums, not database.get_stats() --
    that runs about twenty separate COUNT(*) scans, which is fine for a
    one-shot CLI report and wasteful for something a browser polls.

    `priced_attempts` (rows with a recorded apply_cost_usd) is deliberately
    not `applied + failed`: Retry flips a failed row's apply_status back to
    'queued' without clearing its historical cost, so `applied + failed`
    alone would shrink on a retry while `spend` stayed put -- inflating
    avg-cost-per-attempt for a click that hasn't spent anything yet.

    `spend`/`priced_attempts` are goose-only. Claude is a rarely-used
    fallback backend that costs ~30x more per job (subscription-quota
    Claude Code vs. a cheap OpenRouter model) -- a handful of Claude
    attempts mixed into the average swamps it (measured: $1.16 avg with 3
    Claude rows vs $0.001 for goose), which misrepresents what the actual
    default backend costs going forward.
    """
    conn = conn or get_connection()
    row = conn.execute("""
        SELECT COUNT(*)                                                  AS total,
               SUM(apply_status = 'applied')                             AS applied,
               -- Excludes apply_backend='manual' (self-reported applications
               -- the candidate made by hand, found via scan_gmail_status.py's
               -- NEWJOB detection) -- those aren't a bot attempt, so folding
               -- them into 'applied' here would inflate the success-rate
               -- donut with jobs the bot never touched. 'applied' above still
               -- includes them, since that tile is meant to show total jobs
               -- applied to this cycle regardless of who did it.
               SUM(apply_status = 'applied' AND (apply_backend IS NULL OR apply_backend != 'manual'))
                                                                          AS bot_applied,
               -- Excludes precheck/browser-discovered expired postings: an
               -- expired listing was never a bot failure to begin with (the
               -- posting was gone before a worker even tried), so counting
               -- it toward 'failed' understated the success rate for every
               -- job the bot actually attempted. Tracked separately below.
               SUM(apply_status = 'failed' AND apply_error_category != 'expired')
                                                                          AS failed,
               SUM(apply_status = 'failed' AND apply_error_category = 'expired')
                                                                          AS expired,
               SUM(apply_status = 'queued')                              AS queued,
               SUM(apply_status = 'in_progress')                         AS in_progress,
               SUM(apply_status = 'manual')                              AS manual,
               SUM(fit_score IS NOT NULL)                                AS scored,
               SUM(detail_scraped_at IS NULL AND duplicate_of IS NULL)      AS pending_enrich,
               SUM(review_status = 'needs_review')                       AS needs_review,
               COALESCE(SUM(apply_cost_usd) FILTER (WHERE apply_backend = 'goose'), 0)
                                                                          AS spend,
               SUM(apply_cost_usd IS NOT NULL AND apply_backend = 'goose')
                                                                          AS priced_attempts,
               COALESCE(SUM(score_cost_usd), 0)                          AS scoring_spend,
               SUM(score_cost_usd IS NOT NULL)                           AS priced_scores
        FROM jobs
    """).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def applications(status: str | None = None, limit: int = 200,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    """The Dashboard tab's table: everything that has an apply outcome."""
    conn = conn or get_connection()
    where = "apply_status IS NOT NULL"
    params: list = []
    # 'expired' isn't a real apply_status (precheck failures are still stored
    # as 'failed' -- see stats()'s same split), so it needs its own clause
    # rather than the plain equality below. 'failed' excludes them the same
    # way stats() does, so the Applications tab's Failed pill matches what
    # the dashboard tiles call a failure.
    if status == "expired":
        where += " AND apply_status = 'failed' AND apply_error_category = 'expired'"
    elif status == "failed":
        where += " AND apply_status = 'failed' AND (apply_error_category IS NULL OR apply_error_category != 'expired')"
    elif status:
        where += " AND apply_status = ?"
        params.append(status)
    rows = conn.execute(
        f"""SELECT {_ROW_COLUMNS}, apply_attempts, apply_backend, review_status,
                   last_attempted_at, apply_duration_ms, resume_variant, application_url
            FROM jobs WHERE {where}
            ORDER BY COALESCE(last_attempted_at, applied_at) DESC, url
            LIMIT ?""",
        params + [max(1, min(limit, 1000))],
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Remote enrichment -- the enrichment Pi (a home-IP machine, not this VM's
# datacenter IP; see enrichment/detail.py's `remote_report` param docstring
# for why) has no direct DB access. It pulls pending jobs and reports results
# through these three functions instead of raw SQL. `report_enrich_result`
# deliberately mirrors `scrape_site_batch`'s local UPDATE branches
# byte-for-byte -- any drift between the two would mean a job scraped from
# the Pi ages/retries differently than one scraped locally would have.
# ---------------------------------------------------------------------------

# Same site skip-list detail.py's own local scraper uses -- these sites are
# never worth a detail-page visit (Glassdoor/Google gate behind their own
# login wall, Workopolis' listing already carries everything worth having).
_SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}


def pending_enrich_sites(conn: sqlite3.Connection | None = None) -> list[str]:
    """Distinct sites that currently have at least one pending-enrichment job."""
    conn = conn or get_connection()
    skip_filter = " AND ".join(f"site != '{s}'" for s in _SKIP_DETAIL_SITES)
    rows = conn.execute(
        f"SELECT DISTINCT site FROM jobs WHERE detail_scraped_at IS NULL AND duplicate_of IS NULL AND {skip_filter}"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def pending_enrich_batch(site: str, limit: int = 100,
                         conn: sqlite3.Connection | None = None) -> list[list]:
    """(url, title) pairs for one site's pending-enrichment queue, oldest first.

    Shape matches what `scrape_site_batch`'s `jobs` param already expects, so
    the Pi runner can hand this straight through unchanged.
    """
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT url, title FROM jobs WHERE detail_scraped_at IS NULL AND duplicate_of IS NULL AND site = ? "
        "ORDER BY discovered_at ASC LIMIT ?",
        (site, max(1, min(limit, 500))),
    ).fetchall()
    return [[r[0], r[1]] for r in rows]


def report_enrich_result(url: str, outcome: dict,
                         conn: sqlite3.Connection | None = None) -> dict:
    """Apply one job's remote-scrape outcome, mirroring scrape_site_batch's
    local branches exactly (see that function's `remote_report` docstring).

    outcome["status"] is one of "success" | "network_down" | "error".
    """
    conn = conn or get_connection()
    now = datetime.now(timezone.utc).isoformat()
    status = outcome.get("status")

    if status == "success":
        from applypilot.dedup import check_duplicate
        full_description = outcome.get("full_description")
        application_url = outcome.get("application_url")
        employer_posted_date = outcome.get("employer_posted_date")
        posted_date = outcome.get("posted_date")
        detected = outcome.get("ats")
        conn.execute(
            "UPDATE jobs SET full_description = ?, application_url = ?, "
            "employer_posted_date = ?, posted_date = COALESCE(posted_date, ?), "
            "detail_scraped_at = ?, detail_error = NULL, ats = ? WHERE url = ?",
            (full_description, application_url, employer_posted_date, posted_date,
             now, detected, url),
        )
        conn.commit()
        dup = check_duplicate(conn, url)
        return {"ok": True, "duplicate_of": dup.get("duplicate_of")}

    if status == "network_down":
        # The Pi's own connectivity, not the job's fault -- don't spend an
        # attempt or mark it scraped, same as the local branch.
        conn.execute(
            "UPDATE jobs SET detail_error = ? WHERE url = ?",
            (outcome.get("error"), url),
        )
        conn.commit()
        return {"ok": True}

    # status == "error"
    err = outcome.get("error") or "unknown"
    prev_attempts = conn.execute(
        "SELECT detail_attempts FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    attempts = ((prev_attempts[0] if prev_attempts else 0) or 0) + 1
    if attempts >= 3:
        conn.execute(
            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ?, "
            "detail_attempts = ? WHERE url = ?",
            (err, now, attempts, url),
        )
    else:
        conn.execute(
            "UPDATE jobs SET detail_error = ?, detail_attempts = ? WHERE url = ?",
            (err, attempts, url),
        )
    conn.commit()
    return {"ok": True, "attempts": attempts, "gave_up": attempts >= 3}


NETWORK_STATS_LOG = Path("logs/network_stats.jsonl")  # same file launcher._log_network_stats appends to


def data_stats(path: Path | None = None, bins: int = 12) -> dict:
    """Per-job proxy bytes from network_stats.jsonl (dry runs excluded).

    Read straight from the JSONL rather than a jobs column: it already carries
    ats/url/ts, so no migration. Ceiling: re-reads the whole file per request.
    """
    path = path or NETWORK_STATS_LOG
    mbs: list[float] = []
    by_ats: dict[str, list[float]] = {}
    by_day: dict[str, float] = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        b = r.get("total_bytes")
        if r.get("dry_run") or not b:
            continue
        mb = b / 1e6
        mbs.append(mb)
        by_ats.setdefault(r.get("ats") or "unknown", []).append(mb)
        day = (r.get("ts") or "")[:10]
        by_day[day] = by_day.get(day, 0.0) + mb / 1000
    out: dict = {"jobs": len(mbs), "total_gb": sum(mbs) / 1000,
                 "avg_by_ats": {a: sum(v) / len(v) for a, v in by_ats.items()},
                 "daily_gb": [{"day": d, "gb": g} for d, g in sorted(by_day.items())[-14:]]}
    if not mbs:
        return {**out, "avg_mb": 0, "p50_mb": 0, "p90_mb": 0, "max_mb": 0, "histogram": []}
    s = sorted(mbs)

    def pct(q):
        return s[min(len(s) - 1, int(q * len(s)))]
    # Equal-width bins up to p95 (last bin also holds the outlier tail), so one
    # 40MB job doesn't flatten the whole 2-3MB distribution into a single bar.
    hi = max(pct(0.95), 0.1)
    width = hi / bins
    hist = [0] * bins
    for m in s:
        hist[min(bins - 1, int(m / width))] += 1
    return {**out, "avg_mb": sum(s) / len(s), "p50_mb": pct(0.5), "p90_mb": pct(0.9),
            "max_mb": s[-1],
            "histogram": [{"lo": i * width, "hi": (i + 1) * width, "n": n} for i, n in enumerate(hist)]}


IP_HEALTH_LOG = Path("logs/ip_health.jsonl")  # same file apply/ip_health.log_job appends to
# IPQS calls 75+ "suspicious"; scores above this render red. Override per deploy.
FRAUD_CUTOFF = int(os.environ.get("IP_FRAUD_CUTOFF", 75))


def ip_stats(path: Path | None = None, cutoff: int = FRAUD_CUTOFF) -> dict:
    """Exit-IP health from ip_health.jsonl: fraud-score distribution plus
    block/success rate sliced by provider, ISP, country and city.

    Block rate is the number that matters -- the fraud score is only a proxy
    for it. Ceiling: re-reads the whole file per request, like data_stats.
    """
    path = path or IP_HEALTH_LOG
    try:
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    except FileNotFoundError:
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    scores = [r["fraud_score"] for r in rows if r.get("fraud_score") is not None]
    hist = [0] * 10
    for s in scores:
        hist[min(9, int(s // 10))] += 1

    def group(field: str) -> list[dict]:
        g: dict[str, list[dict]] = {}
        for r in rows:
            g.setdefault(r.get(field) or "unknown", []).append(r)
        out = []
        for name, rs in g.items():
            sc = [r["fraud_score"] for r in rs if r.get("fraud_score") is not None]
            out.append({
                "name": name, "n": len(rs),
                "ips": len({r["ip"] for r in rs if r.get("ip")}),
                "success_rate": sum(r["applied"] for r in rs) / len(rs),
                "block_rate": sum(r["blocked"] for r in rs) / len(rs),
                "avg_score": sum(sc) / len(sc) if sc else None,
            })
        return sorted(out, key=lambda x: -x["n"])[:15]

    # Shown as soon as Webshare is configured, before the first swap exists.
    from applypilot.apply import webshare
    swaps = webshare.state() if webshare.enabled() else None
    return {
        "swaps": swaps,
        "jobs": len(rows), "ips": len({r["ip"] for r in rows if r.get("ip")}),
        "scored": len(scores), "cutoff": cutoff,
        "avg_score": sum(scores) / len(scores) if scores else None,
        "pct_over_cutoff": sum(s >= cutoff for s in scores) / len(scores) if scores else None,
        "block_rate": sum(r["blocked"] for r in rows) / len(rows) if rows else None,
        "histogram": hist,
        "groups": {f: group(f) for f in ("provider", "isp", "country", "city")},
    }
