"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,
            job_type              TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,
            detail_attempts       INTEGER DEFAULT 0,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,
            requires_returning_student TEXT,
            is_terminal_internship TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,
            resume_variant        TEXT,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,
            review_status         TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    # Indexes come after the columns they cover, since several are composite
    # over columns that only arrive via ensure_columns on an older database.
    ensure_indexes(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    "job_type": "TEXT",
    # Best-effort posting date, as scraped from the card text ("2 days ago",
    # "Aug 28, 2026", etc.) and normalized to ISO 8601. Used for the age-decay
    # ordering in the apply queue -- discovered_at alone conflates "how long
    # we've known about this" with "how long it's actually been open", and a
    # posting can be up to 7 days old on first discovery (see
    # _posted_within_days). Null when the source page gave no parseable date.
    "posted_date": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    "detail_attempts": "INTEGER DEFAULT 0",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Which academic term this role runs in, read by the LLM at scoring time
    # from the posting itself (title, dates, description) -- spring / summer
    # / fall / winter / rolling / unclear. Feeds both the RETURNING STUDENT
    # CHECK (a role that ends before the candidate's graduation can't
    # conflict with an enrollment requirement) and is_remote_spring_internship.
    "term": "TEXT",
    "requires_returning_student": "TEXT",
    # The LLM's own read of whether the posting affirmatively welcomes an
    # already-graduated candidate (TERMINAL EVIDENCE CHECK in the scoring
    # prompt) -- primary signal for is_terminal_internship. The regex-based
    # terminal_evidence()/TERMINAL_EXCLUDE_RE in scorer.py stays on as a
    # safety-net veto, not the primary detector: regex kept missing real
    # cases (co-ops, "must have attained a degree", garbled phrasing) that
    # needed actual reading comprehension, and re-patching one regex pattern
    # per newly-discovered case doesn't scale. NULL for jobs scored before
    # this field existed -- compute_terminal_internships() falls back to the
    # regex alone for those until they're re-scored.
    "terminal_evidence_llm": "TEXT",
    "is_terminal_internship": "TEXT",
    # A Spring-term internship that's fully remote is, for a candidate who
    # graduates in May, just as safe to auto-send as a confirmed terminal
    # summer internship -- the term ends at or before graduation, so there's
    # no returning-student conflict to begin with, and remote means no
    # relocation/on-campus conflict either. Same fit/desirability bar as
    # is_terminal_internship (compute_remote_spring_internships()); the two
    # flags are combined with OR, never added, when ranking the apply queue,
    # so a role that happens to satisfy both isn't double-boosted.
    "is_remote_spring_internship": "TEXT",
    # A softer sibling of is_terminal_internship for postings that never say
    # anything either way about post-grad eligibility -- no explicit welcome
    # phrase (which would already make is_terminal_internship 'yes') and no
    # explicit return-to-school requirement (which would exclude them
    # entirely). Otherwise-strong postings this silent about it are, by base
    # rate, far more often "doesn't matter" than "will auto-reject you" --
    # see compute_likely_terminal_internships(). Informational only: unlike
    # is_terminal_internship, this does NOT jump the apply queue.
    "is_terminal_internship_likely": "TEXT",
    # ATS keywords from the job description that match or could match the
    # candidate, extracted at scoring time (same Gemini call, no extra cost).
    # Was previously folded into score_reasoning as an unstructured first
    # line; broken out so the apply prompt can use it directly for the
    # skills-field augmentation without string-parsing reasoning text.
    "keywords": "TEXT",
    # The hiring company, as named in the posting. Most discovery sources
    # can't supply this -- `site` is the board we found the job on ("Intern
    # List - SWE"), not the employer -- so the scorer extracts it from the
    # description alongside the score, in the same LLM call rather than a
    # second pass. JobSpy is the exception: it returns the employer as a
    # structured field, so that path writes it at discovery time and the
    # scorer is handed the name instead of re-deriving it.
    "company": "TEXT",
    # 1-10 brand/reputation judgement, an input to desirability_score only.
    # Never affects fit_score, which stays a pure skill match.
    "company_prestige": "INTEGER",
    # Hard eligibility gate: 'yes' | 'no' | 'unclear'. Only a stated, hard
    # disqualifier (degree level, class year, non-US, clearance) earns a 'no';
    # 'unclear' is treated as eligible, because a false 'no' silently costs an
    # opportunity while a false 'yes' only risks one apply run.
    "eligible": "TEXT",
    "eligibility_reason": "TEXT",
    # Computed from company_prestige + location + salary with no LLM call, so
    # re-tuning the weights is free and never needs a re-score.
    "desirability_score": "REAL",
    # 'tier1' | 'adjacent' | NULL. Recomputed from the company name against
    # config.TIER1_COMPANIES / TIER1_ADJACENT (see
    # scoring.scorer.compute_company_tiers). Stored rather than derived in
    # SQL because name matching needs normalization -- a LIKE 'block%'
    # pattern would happily match "Blockchain Widgets Inc".
    "company_tier": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    "resume_variant": "TEXT",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    # NULL | 'queued' | 'in_progress' | 'applied' | 'failed' | 'manual'
    # | 'captcha' | 'expired' | 'login_issue'  (see apply/outcomes.py).
    # 'queued' is written only by the web UI when the user confirms a batch;
    # 'in_progress' is a live lock held by a worker and carries agent_id.
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    "review_status": "TEXT",
    # Which backend drove this application, and how many LLM requests it took.
    # Together with apply_duration_ms these make backend/model comparison
    # measurable instead of anecdotal.
    # Pay as stated in the posting, and whether it falls under the candidate's
    # floor. Judged at scoring time (free) so below-floor jobs never reach the
    # apply stage; 'unknown' when the posting states no pay, which is common.
    # Which ATS the posting routes to. Detected by pattern match (no LLM call),
    # and re-detected at apply time when the real URL is known -- job boards
    # front the ATS behind redirects.
    "ats": "TEXT",
    "pay_text": "TEXT",
    "pay_below_floor": "TEXT",
    "apply_backend": "TEXT",
    "apply_llm_requests": "INTEGER",
    # Token accounting. The stream already reports these; recording them is what
    # makes "are we paying for input or output" answerable, and shows whether
    # prompt caching is actually working (cache_read should dominate).
    "apply_input_tokens": "INTEGER",
    "apply_output_tokens": "INTEGER",
    "apply_cache_read_tokens": "INTEGER",
    "apply_cost_usd": "REAL",
    # Selection queue. The web UI is the only writer: confirming a batch stamps
    # each chosen row with the batch id and its position and sets apply_status
    # to 'queued'. acquire_job() in queued mode then drains exactly that set in
    # exactly that order, skipping its own ranking entirely -- when a human has
    # picked the jobs, their selection IS the ranking, and re-gating it on
    # fit/pay/eligibility would silently drop rows they explicitly chose.
    # Stated pay normalised to dollars per hour so one threshold works across
    # "/hr", "/wk", "/mon" and "/yr" postings. See applypilot/pay.py. -1 means
    # "parsed and unusable" (a non-dollar currency, or "N/A"); NULL means not
    # looked at yet, and the two must stay distinct or every startup re-parses
    # the same unparseable strings.
    "pay_min_hourly": "REAL",
    "pay_max_hourly": "REAL",
    "queued_at": "TEXT",
    "queue_batch": "TEXT",
    "queue_position": "INTEGER",
}


# Indexes. The table ran without any of these until the web UI existed, which
# was survivable while the only reader was a CLI doing one full pass per stage.
# The browse view reads a single day at a time, filtered and sorted, many times
# per session -- that is a different access pattern and it needs support.
#
# The day expression must be written character-for-character the way the query
# writes it, or SQLite will not match the index to the query.
_DAY_EXPR = "date(COALESCE(posted_date, discovered_at))"

_ALL_INDEXES: dict[str, str] = {
    # Day bucketing: the browse tab groups by this and nothing else.
    "idx_jobs_day": f"({_DAY_EXPR})",
    # The two quick chips, each a sort within one day. Composite so the day
    # filter and the ordering are served by one index instead of a scan+sort.
    "idx_jobs_day_prestige": f"({_DAY_EXPR}, company_prestige DESC)",
    "idx_jobs_day_fit": f"({_DAY_EXPR}, fit_score DESC)",
    # Dashboard tab and the stale-lock sweep both filter on status.
    "idx_jobs_apply_status": "(apply_status)",
    # Draining one batch in order.
    "idx_jobs_queue": "(queue_batch, queue_position)",
    # Pipeline backlog counts, run every time the stats endpoint is polled.
    "idx_jobs_detail_pending": "(detail_scraped_at)",
    "idx_jobs_scored": "(scored_at)",
    "idx_jobs_site": "(site)",
    "idx_jobs_pay": "(pay_max_hourly)",
    # Big-tech postings are pinned above the ranking in every browse view.
    "idx_jobs_day_tier": f"({_DAY_EXPR}, company_tier)",
}


def ensure_indexes(conn: sqlite3.Connection | None = None) -> list[str]:
    """Create any missing indexes on the jobs table.

    Idempotent, like ensure_columns -- CREATE INDEX IF NOT EXISTS means this is
    safe on every startup. Returns the names that did not already exist, which
    is only useful for logging; an index that was already there is not an
    error.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of index names created (empty if all were already present).
    """
    if conn is None:
        conn = get_connection()

    existing = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    created = []

    for name, cols in _ALL_INDEXES.items():
        if name not in existing:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON jobs {cols}")
            created.append(name)

    if created:
        conn.commit()

    return created


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Last discovery per site -- lets `status` show "how stale is each
    # source" without needing to actually re-run discovery to find out.
    rows = conn.execute(
        "SELECT site, MAX(discovered_at) FROM jobs GROUP BY site"
    ).fetchall()
    stats["last_discovered_by_site"] = [(row[0], row[1]) for row in rows]

    # Last activity timestamps for enrich/score -- these plus the running-
    # process check (see is_stage_running() in cli.py) are what makes
    # `applypilot status` show real pipeline activity instead of just a
    # point-in-time count.
    stats["last_enrich_at"] = conn.execute(
        "SELECT MAX(detail_scraped_at) FROM jobs"
    ).fetchone()[0]
    stats["last_score_at"] = conn.execute(
        "SELECT MAX(scored_at) FROM jobs"
    ).fetchone()[0]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Same distribution split by internship vs new_grad -- the raw top-tier
    # count differs enough between the two (internships routinely score
    # higher in volume) that a plain "highest score wins" apply order would
    # silently skew toward one type without this ever being visible.
    type_dist_rows = conn.execute(
        "SELECT job_type, fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL AND fit_score >= 7 "
        "GROUP BY job_type, fit_score ORDER BY job_type, fit_score DESC"
    ).fetchall()
    stats["score_distribution_by_type"] = [
        (row[0] or "unknown", row[1], row[2]) for row in type_dist_rows
    ]

    # Total scored per type regardless of score -- scale context for the 7+
    # breakdown above, so e.g. "270 internships scored 7+" reads against
    # "out of 700 scored" rather than floating with no denominator.
    total_scored_rows = conn.execute(
        "SELECT job_type, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL GROUP BY job_type"
    ).fetchall()
    stats["total_scored_by_type"] = {
        (row[0] or "unknown"): row[1] for row in total_scored_rows
    }

    # Terminal internships -- ones that don't require returning to school,
    # functionally a new-grad bridge role -- get guaranteed top apply
    # priority (see acquire_job()). Surfaced on its own rather than folded
    # into the score distribution, since it's an orthogonal flag, not a tier.
    stats["terminal_internships"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_terminal_internship = 'yes'"
    ).fetchone()[0]

    # Postings that clear the same fit/desirability bar as a confirmed
    # terminal internship but never say anything about post-grad eligibility
    # either way -- see compute_likely_terminal_internships(). Informational
    # only; unlike terminal_internships this does not jump the apply queue.
    stats["likely_terminal_internships"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_terminal_internship_likely = 'yes'"
    ).fetchone()[0]

    # Same top-priority tier as terminal_internships, via a different route
    # (see is_remote_spring_internship's column comment) -- surfaced
    # separately since it's a different signal, not folded into the count.
    stats["remote_spring_internships"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_remote_spring_internship = 'yes'"
    ).fetchone()[0]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def fit_gate_sql(min_score: int) -> tuple[str, list]:
    """SQL for "worth applying to", plus its bind params.

    Normally this is just `fit_score >= min_score`, but a strong enough
    employer earns a shot at a lower bar: a prestigious company is worth an
    application even on an imperfect match, because the downside is one
    application's cost and the upside is asymmetric.

    The tiers come from `prestige_override_tiers` in settings.json as
    [min_prestige, min_fit] pairs, so the trade can be retuned (or switched
    off with an empty list) without touching code.

    Shared by the tailor stage and the apply queue deliberately: if only the
    queue knew about the override, the extra jobs would never get a resume
    attached and so could never actually be picked up.
    """
    from applypilot.config import load_settings

    clauses = ["fit_score >= ?"]
    params: list = [min_score]
    for tier in load_settings().get("prestige_override_tiers", []) or []:
        try:
            min_prestige, min_fit = int(tier[0]), int(tier[1])
        except (TypeError, ValueError, IndexError):
            continue
        clauses.append("(company_prestige >= ? AND fit_score >= ?)")
        params.extend([min_prestige, min_fit])

    return "(" + " OR ".join(clauses) + ")", params


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if stage == "pending_tailor":
        # The fit bar for tailoring isn't a single comparison any more -- a
        # prestigious employer qualifies at a lower score (see fit_gate_sql) --
        # so this stage substitutes its own multi-param clause rather than
        # going through the single-? path below.
        gate_sql, gate_params = fit_gate_sql(min_score if min_score is not None else 7)
        where = where.replace("fit_score >= ?", gate_sql)
        params.extend(gate_params)
    elif "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
