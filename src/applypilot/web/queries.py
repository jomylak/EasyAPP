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

import sqlite3

from applypilot.database import _DAY_EXPR, get_connection

# What the browse table shows per row. Deliberately excludes full_description:
# a day of 400 rows would carry megabytes of prose nobody has expanded yet.
# view.py inlined every description into one page and produced a 21 MB file.
_ROW_COLUMNS = f"""
    url, title, company, site, location, salary, pay_text,
    fit_score, desirability_score, company_prestige,
    job_type, ats, eligible, keywords,
    pay_min_hourly, pay_max_hourly, pay_below_floor,
    apply_status, applied_at, apply_error, apply_cost_usd,
    queue_batch, tailored_resume_path,
    {_DAY_EXPR} AS day,
    COALESCE(posted_date, discovered_at) AS posted
"""

# Sortable columns, mapped to SQL. A whitelist rather than interpolation --
# the sort key arrives from a query string.
SORTS: dict[str, str] = {
    "prestige": "company_prestige DESC, fit_score DESC",
    "fit": "fit_score DESC, desirability_score DESC",
    "desirability": "desirability_score DESC, fit_score DESC",
    "company": "company COLLATE NOCASE ASC",
    "title": "title COLLATE NOCASE ASC",
    "posted": "COALESCE(posted_date, discovered_at) DESC",
    # Never sort on the `salary` text: it compares "$9" against "$110500"
    # lexically and puts the nine first.
    "pay": "pay_max_hourly DESC NULLS LAST",
}
DEFAULT_SORT = "prestige"


def _filter_clauses(f: dict) -> tuple[str, list]:
    """Translate the UI's filter dict into SQL. Unknown keys are ignored."""
    clauses, params = [], []

    if f.get("day"):
        clauses.append(f"{_DAY_EXPR} = ?")
        params.append(f["day"])
    if f.get("min_fit") is not None:
        clauses.append("fit_score >= ?")
        params.append(f["min_fit"])
    if f.get("min_desirability") is not None:
        clauses.append("desirability_score >= ?")
        params.append(f["min_desirability"])
    if f.get("min_prestige") is not None:
        clauses.append("company_prestige >= ?")
        params.append(f["min_prestige"])
    if f.get("min_pay") is not None:
        # Compared against the *high* end: "pay >= 40" asks which jobs could
        # pay at least that, and a $30-$60 posting qualifies. -1 is the
        # "unparseable" marker and NULL is "no stated pay"; both are excluded,
        # because a threshold the user typed is a deliberate act and silently
        # including unknowns would defeat it.
        clauses.append("pay_max_hourly IS NOT NULL AND pay_max_hourly >= ?")
        params.append(f["min_pay"])
    if f.get("job_type"):
        clauses.append("job_type = ?")
        params.append(f["job_type"])
    if f.get("site"):
        clauses.append("site = ?")
        params.append(f["site"])
    if f.get("ats"):
        clauses.append("ats = ?")
        params.append(f["ats"])
    if f.get("eligible_only"):
        # NULL passes: a job scored before the eligibility gate existed is not
        # known to be ineligible, and dropping it would hide real openings.
        clauses.append("(eligible IS NULL OR eligible != 'no')")
    if f.get("above_pay_floor"):
        clauses.append("(pay_below_floor IS NULL OR pay_below_floor != 'yes')")
    if f.get("unapplied_only"):
        clauses.append("(apply_status IS NULL OR apply_status = 'failed')")
    if f.get("posted_within_days") is not None:
        # Matches the expression `idx_jobs_day` is built on byte-for-byte, so
        # this range scan can use that index instead of a full table scan.
        clauses.append(f"{_DAY_EXPR} >= date('now', ?)")
        params.append(f"-{int(f['posted_within_days'])} days")
    if f.get("q"):
        clauses.append("(title LIKE ? OR company LIKE ? OR keywords LIKE ?)")
        like = f"%{f['q']}%"
        params += [like, like, like]

    return (" AND ".join(clauses) if clauses else "1"), params


def list_days(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every day that has postings, newest first, with the chip counts.

    The counts come back with the days rather than from three more round
    trips, because the chips are labelled before anyone clicks them.
    """
    conn = conn or get_connection()
    rows = conn.execute(f"""
        SELECT {_DAY_EXPR} AS day,
               COUNT(*) AS total,
               SUM(CASE WHEN company_prestige >= 9 THEN 1 ELSE 0 END) AS prestige,
               SUM(CASE WHEN desirability_score >= 6 AND fit_score >= 8
                        THEN 1 ELSE 0 END) AS best_fit,
               SUM(CASE WHEN apply_status = 'applied' THEN 1 ELSE 0 END) AS applied
        FROM jobs
        WHERE {_DAY_EXPR} IS NOT NULL
        GROUP BY day
        ORDER BY day DESC
    """).fetchall()
    return [dict(r) for r in rows]


def list_jobs(filters: dict, sort: str = DEFAULT_SORT,
              page: int = 0, page_size: int = 30,
              conn: sqlite3.Connection | None = None) -> dict:
    """One page of one day's table."""
    conn = conn or get_connection()
    where, params = _filter_clauses(filters)
    order = SORTS.get(sort, SORTS[DEFAULT_SORT])
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
    }


def job_detail(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Everything the expanded row shows, including the description."""
    conn = conn or get_connection()
    row = conn.execute(
        f"SELECT {_ROW_COLUMNS}, full_description, description, application_url,"
        f" score_reasoning, resume_variant, review_status, apply_attempts,"
        f" is_terminal_internship, requires_returning_student, eligibility_reason"
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

    return job


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
    """
    conn = conn or get_connection()
    row = conn.execute("""
        SELECT COUNT(*)                                                  AS total,
               SUM(apply_status = 'applied')                             AS applied,
               SUM(apply_status = 'failed')                              AS failed,
               SUM(apply_status = 'queued')                              AS queued,
               SUM(apply_status = 'in_progress')                         AS in_progress,
               SUM(apply_status = 'manual')                              AS manual,
               SUM(fit_score IS NOT NULL)                                AS scored,
               SUM(detail_scraped_at IS NULL)                            AS pending_enrich,
               SUM(review_status = 'needs_review')                       AS needs_review,
               COALESCE(SUM(apply_cost_usd), 0)                          AS spend,
               SUM(apply_cost_usd IS NOT NULL)                           AS priced_attempts
        FROM jobs
    """).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def applications(status: str | None = None, limit: int = 200,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    """The Dashboard tab's table: everything that has an apply outcome."""
    conn = conn or get_connection()
    where = "apply_status IS NOT NULL"
    params: list = []
    if status:
        where += " AND apply_status = ?"
        params.append(status)
    rows = conn.execute(
        f"""SELECT {_ROW_COLUMNS}, apply_attempts, apply_backend, review_status,
                   last_attempted_at, apply_duration_ms, resume_variant
            FROM jobs WHERE {where}
            ORDER BY COALESCE(last_attempted_at, applied_at) DESC, url
            LIMIT ?""",
        params + [max(1, min(limit, 1000))],
    ).fetchall()
    return [dict(r) for r in rows]
