"""Apply orchestration: acquire jobs, drive them via a backend, track results.

This is the main entry point for the apply pipeline. It pulls jobs from the
database, launches Chrome for each one, hands the job to the selected apply
backend (see `applypilot.apply.backends`), and writes the outcome back.
Supports parallel workers via --workers.

Orchestration here is backend-agnostic: job acquisition, Chrome lifecycle,
the dashboard, and retry/review classification are shared, while *how* the
browser gets driven is the backend's business.
"""

import atexit
import json
import logging
import platform
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import outcomes, prompt as prompt_mod
from applypilot.apply.backends import get_backend, interrupt_all_backends
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    cleanup_on_exit, BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, render_full, get_totals,
    begin_run, end_run,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

# Columns every acquire_job branch returns. Kept in one place because the
# branches differ only in how they pick a row, never in what the caller gets.
_JOB_COLUMNS = """url, title, site, company, application_url,
                  tailored_resume_path, fit_score, location, full_description,
                  cover_letter_path, keywords, apply_status AS prior_status,
                  applied_at AS prior_applied_at"""

# How many unusable rows (manual ATS, blocked site) one acquire_job call will
# set aside before giving up. Bounded so a selection made entirely of unusable
# rows ends the worker instead of spinning against the database.
_MAX_DEFERRALS = 50


def _daily_cap_reason(conn, settings: dict) -> str | None:
    """Whether today's spend or application count has hit a configured cap.

    Both caps are soft stops, checked once per acquire_job call rather than
    enforced mid-run: a job already `in_progress` finishes normally (killing
    it mid-application would waste the money already spent getting it that
    far), and this just stops the next claim from being made. `None` (the
    default) on either setting means no cap.

    Spend and count are read from the `jobs` table itself, not a separate
    counter -- `apply_cost_usd` and `apply_status` are already the durable
    record `mark_result` writes, so "today's totals" is just today's slice of
    that, with no new bookkeeping to keep in sync.
    """
    max_spend = settings.get("max_daily_spend_usd")
    max_count = settings.get("max_daily_applications")
    if max_spend is None and max_count is None:
        return None
    row = conn.execute("""
        SELECT COALESCE(SUM(apply_cost_usd), 0) AS spend,
               SUM(apply_status IN ('applied', 'failed')) AS attempts
        FROM jobs
        WHERE date(COALESCE(last_attempted_at, applied_at)) = date('now')
    """).fetchone()
    spend, attempts = row["spend"] or 0.0, row["attempts"] or 0
    if max_spend is not None and spend >= max_spend:
        return f"daily spend cap reached (${spend:.2f} >= ${max_spend:.2f})"
    if max_count is not None and attempts >= max_count:
        return f"daily application cap reached ({attempts} >= {max_count})"
    return None


def _like_to_substring(pattern: str) -> str:
    """Turn a SQL LIKE pattern from sites.yaml into a plain substring.

    The patterns are all of the form %fragment%, and they are matched in SQL
    for the ranked branch. The queued branch has to make the same judgement in
    Python, and this keeps the two answering from the same config rather than
    from a second hand-maintained list.
    """
    return pattern.strip().strip("%").lower()


def _is_blocked(site: str | None, url: str | None,
                blocked_sites: list, blocked_patterns: list) -> bool:
    """Whether a row is one the apply agent is configured never to touch."""
    if site and site in blocked_sites:
        return True
    lowered = (url or "").lower()
    return any(frag and frag in lowered
               for frag in map(_like_to_substring, blocked_patterns))


def _select_target(conn, target_url: str):
    """Pick one specific job by URL, for `applypilot apply --url`."""
    like = f"%{target_url.split('?')[0].rstrip('/')}%"
    return conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
          AND tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status != 'in_progress')
        LIMIT 1
    """, (target_url, target_url, like, like)).fetchone()


def _select_queued(conn, queue_batch: str, deferred: set):
    """Pick the next job from a batch the user selected in the web UI.

    Deliberately applies none of the ranked branch's gates -- not the fit
    threshold, not the pay floor, not eligibility, not age decay. A human
    looked at these rows and chose them, so their selection *is* the ranking,
    and re-filtering it here would silently drop jobs they explicitly picked
    and leave the batch permanently short of finishing.
    """
    params = [queue_batch]
    skip_clause = ""
    if deferred:
        skip_clause = f"AND url NOT IN ({','.join('?' * len(deferred))})"
        params.extend(sorted(deferred))
    return conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE queue_batch = ?
          AND apply_status = 'queued'
          {skip_clause}
        ORDER BY queue_position, url
        LIMIT 1
    """, params).fetchone()


def _select_ranked(conn, min_score: int, skip: set,
                   blocked_sites: list, blocked_patterns: list):
    """Pick the highest-ranked job the pipeline thinks is worth applying to."""
    _settings = config.load_settings()
    # Build parameterized filters to avoid SQL injection
    from applypilot.database import fit_gate_sql
    fit_gate, params = fit_gate_sql(min_score)
    seen_clause = ""
    if skip:
        placeholders = ",".join("?" * len(skip))
        seen_clause = f"AND url NOT IN ({placeholders})"
        params.extend(sorted(skip))
    site_clause = ""
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND site NOT IN ({placeholders})"
        params.extend(blocked_sites)
    url_clauses = ""
    if blocked_patterns:
        url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
        params.extend(blocked_patterns)
    return conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed')
          -- Pay below the candidate's floor is decided at scoring time
          -- (free) rather than burning an apply run to discover it.
          -- NULL/'unknown' still applies: most postings state no pay.
          AND (pay_below_floor IS NULL OR pay_below_floor != 'yes')
          -- Hard eligibility (degree level, class year, non-US,
          -- clearance) decided at scoring time. NULL passes so jobs
          -- scored before this gate existed aren't silently dropped,
          -- and 'unclear' passes because a wrong reject costs a real
          -- opportunity while a wrong accept costs one apply run.
          AND (eligible IS NULL OR eligible != 'no')
          AND (apply_attempts IS NULL OR apply_attempts < ?)
          AND {fit_gate}
          {seen_clause}
          {site_clause}
          {url_clauses}
        -- Terminal internships (don't require returning to school) and
        -- remote-spring internships (term ends before graduation, so the
        -- question never comes up) are two different routes to the same
        -- guarantee: a role the candidate can honestly take right now, no
        -- caveats. Both are rare and have already cleared every gate above
        -- (pay floor, eligibility) by the time we get here, so there's no
        -- reason to hold one back waiting for something hypothetically
        -- better: sort on EITHER flag FIRST, ahead of the score blend
        -- entirely. OR, not added -- a role that happens to satisfy both
        -- is still just one top-priority row, not a double boost.
        --
        -- Below that: big-tech postings, then desirability (which now
        -- carries pay, prestige and location as three genuinely separate
        -- weighted terms), decayed by age. Skill fit is a pure tiebreaker
        -- and nothing more: it used to be the dominant term at weight 0.7
        -- and it buried exactly the postings worth applying to -- Meta
        -- internships average a fit of 3.4 and Anthropic new-grad a 2.0,
        -- both at prestige 10, and not one had ever been applied to.
        --
        -- The old blanket +0.5 nudge for new_grad rows is gone: the
        -- internship/new-grad balance is a selection decision now, made in
        -- the browse UI's two ranked lanes against a running 60/40 counter,
        -- not something for this ORDER BY to guess at.
        ORDER BY
          (CASE WHEN is_terminal_internship = 'yes'
                  OR is_remote_spring_internship = 'yes' THEN 1 ELSE 0 END) DESC,
          (CASE company_tier WHEN 'tier1' THEN 2
                             WHEN 'adjacent' THEN 1 ELSE 0 END) DESC,
          COALESCE(desirability_score, fit_score)
          - (julianday('now') - julianday(COALESCE(posted_date, discovered_at))) * ? DESC,
          fit_score DESC,
          COALESCE(posted_date, discovered_at) DESC,
          url
        LIMIT 1
    """, [_settings.get("max_apply_attempts") or config.DEFAULTS["max_apply_attempts"]] + params
         + [config.DEFAULTS["job_age_decay_per_day"]]).fetchone()


def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0,
                exclude_urls: set[str] | None = None,
                queue_batch: str | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Three ways to choose a row, one way to claim it. The claim is a
    BEGIN IMMEDIATE transaction that flips apply_status to 'in_progress' and
    stamps agent_id, which is the only thing keeping parallel workers off each
    other's jobs.

    Args:
        target_url: Apply to a specific URL instead of picking from a queue.
        min_score: Minimum fit_score threshold. Ranked mode only.
        worker_id: Worker claiming this job (for tracking).
        exclude_urls: URLs already attempted in this session. Needed because a
            dry run deliberately leaves the job's status untouched, so without
            this the same top-scoring job is handed back every iteration.
        queue_batch: Drain this user-selected batch in the order the user put
            it in, ignoring the ranked mode's gates entirely.

    Returns:
        Job dict, or None if there is nothing left to claim.
    """
    conn = get_connection()
    blocked_sites, blocked_patterns = _load_blocked()

    cap_reason = _daily_cap_reason(conn, config.load_settings())
    if cap_reason:
        logger.warning("Stopping acquisition: %s", cap_reason)
        return None

    # Rows this call has set aside after recording a terminal outcome for them.
    # They are excluded from the next iteration's SELECT so the loop advances
    # instead of re-picking the row it just wrote off. Returning None on the
    # first unusable row -- which is what this used to do for a manual-ATS
    # site -- reads to worker_loop as "queue empty" and ends the entire run,
    # so a single unusable row partway down a batch would strand every job
    # behind it.
    deferred: set[str] = set()

    for _ in range(_MAX_DEFERRALS):
        try:
            conn.execute("BEGIN IMMEDIATE")

            if target_url:
                row = _select_target(conn, target_url)
            elif queue_batch:
                row = _select_queued(conn, queue_batch, deferred)
            else:
                row = _select_ranked(conn, min_score,
                                     set(exclude_urls or ()) | deferred,
                                     blocked_sites, blocked_patterns)

            if not row:
                conn.rollback()
                return None

            apply_url = row["application_url"] or row["url"]
            now = datetime.now(timezone.utc).isoformat()

            # Two ways a row can turn out unusable once it is in hand. Both get
            # a terminal status rather than a silent skip, so a user-selected
            # batch always drains and the dashboard can say why a job never
            # ran. The ranked branch filters blocked sites in SQL already, so
            # that check only ever fires for a queued or targeted row.
            from applypilot.config import is_manual_ats
            if is_manual_ats(apply_url):
                outcome = ("manual", "manual ATS")
            elif _is_blocked(row["site"], row["url"],
                             blocked_sites, blocked_patterns):
                outcome = ("failed", "site_blocked")
            else:
                outcome = None

            if outcome:
                status, reason = outcome
                conn.execute(
                    "UPDATE jobs SET apply_status = ?, apply_error = ?, "
                    "agent_id = NULL, last_attempted_at = ? WHERE url = ?",
                    (status, reason, now, row["url"]),
                )
                conn.commit()
                logger.info("Skipping %s (%s): %s", reason, status, row["url"][:80])
                if target_url:
                    # An explicit --url asked for this one row and it is not
                    # applyable. Falling through would re-select it forever.
                    return None
                deferred.add(row["url"])
                continue

            conn.execute("""
                UPDATE jobs SET apply_status = 'in_progress',
                               agent_id = ?,
                               last_attempted_at = ?
                WHERE url = ?
            """, (f"worker-{worker_id}", now, row["url"]))
            conn.commit()

            return dict(row)
        except Exception:
            conn.rollback()
            raise

    logger.warning("Gave up after %d unusable rows in a row (worker %d).",
                   _MAX_DEFERRALS, worker_id)
    return None


# Above this many sibling internships, one company is running more than one
# program and a single form mismatch stops being evidence about the rest.
_SIBLING_SWEEP_MAX = 8


def _clear_terminal_flags_on_grad_date_mismatch(job_url: str) -> None:
    """A grad_date_mismatch means the form itself required a graduation
    timing the candidate's one true resume doesn't satisfy -- i.e. the job
    turned out not to be a "terminal" internship after all, even if it was
    flagged as one (confirmed or likely). Clear both flags so it stops
    getting queue-jump priority its own apply attempt just proved wrong.

    grad_date_mismatch is a PERMANENT_FAILURES reason (see outcomes.py), so
    this job won't be retried -- there's no second resume to swap to any
    more, only the true graduation date. It also corrects other
    not-yet-applied internships at the SAME company: near-identical postings
    (e.g. Verkada's Backend/Frontend/Embedded/Mobile/Security internships)
    routinely share the exact same graduation-date form field, so a
    confirmed mismatch on one is real evidence about all of them, not just
    the one that happened to be tried first. Without this, the pipeline
    would spend a separate wasted apply attempt discovering the identical
    wall on each sibling in turn.

    That inference only holds for a company posting one program through one
    form. It breaks badly at scale: Amazon has 29 distinct internships here
    and Google, Meta and NVIDIA each run several unrelated programs with
    their own eligibility rules, so one mismatch is not evidence about the
    rest -- and killing them outright is the exact opposite of the standing
    requirement that every big-tech posting gets applied to. So a large or
    tier-listed employer is exempted: its siblings are marked 'unclear'
    (still visible, still applyable, flagged for a human look) rather than
    'no', and their terminal flags are left alone. The originating job's own
    flags are cleared either way -- that one really did hit the wall.

    Scoped to rows the acquire_job() gate would otherwise still offer
    (apply_status IS NULL or 'failed', i.e. not 'applied' or 'in_progress')
    -- there's no reason to touch a job already submitted or mid-attempt.
    """
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET is_terminal_internship = 'no', "
            "is_terminal_internship_likely = 'no' WHERE url = ?",
            (job_url,),
        )
        row = conn.execute(
            "SELECT company, company_tier FROM jobs WHERE url = ?", (job_url,)
        ).fetchone()
        company = row["company"] if row else None
        if company:
            sibling_count = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE company = ? "
                "AND job_type = 'internship' AND url != ?",
                (company, job_url),
            ).fetchone()["n"]
            is_tiered = bool(
                row["company_tier"] if "company_tier" in row.keys() else None
            )
            broad_employer = is_tiered or sibling_count > _SIBLING_SWEEP_MAX

            note = (
                "Sibling posting at this company confirmed a graduation-date "
                "form mismatch (grad_date_mismatch)."
                if broad_employer else
                "Sibling posting at this company confirmed a graduation-date "
                "form mismatch (grad_date_mismatch) -- treating this posting "
                "as requiring the same continued enrollment."
            )
            if broad_employer:
                # Flag for review, don't disqualify. Only ever softens a row
                # that is currently 'yes' -- an existing 'no' from the
                # scorer's own eligibility read is a real, separately
                # established disqualifier and must not be undone here.
                conn.execute(
                    """
                    UPDATE jobs SET
                        eligible = 'unclear',
                        eligibility_reason = CASE
                            WHEN eligibility_reason IS NULL OR eligibility_reason = ''
                            THEN ?
                            ELSE eligibility_reason || ' ' || ?
                        END
                    WHERE company = ?
                      AND job_type = 'internship'
                      AND url != ?
                      AND eligible = 'yes'
                      AND (apply_status IS NULL OR apply_status = 'failed')
                    """,
                    (note, note, company, job_url),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs SET
                        is_terminal_internship = 'no',
                        is_terminal_internship_likely = 'no',
                        requires_returning_student = 'yes',
                        eligible = 'no',
                        eligibility_reason = CASE
                            WHEN eligibility_reason IS NULL OR eligibility_reason = ''
                            THEN ?
                            ELSE eligibility_reason || ' ' || ?
                        END
                    WHERE company = ?
                      AND job_type = 'internship'
                      AND url != ?
                      AND (apply_status IS NULL OR apply_status = 'failed')
                    """,
                    (note, note, company, job_url),
                )
        conn.commit()
    except Exception as e:
        logger.warning("Could not clear terminal flags after grad_date_mismatch: %s", e)


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None, backend: str | None = None,
                llm_requests: int | None = None, stats: dict | None = None) -> None:
    """Update a job's apply status in the database.

    Args:
        backend: Which apply backend produced this outcome. Recorded so
            completion rates can be compared between backends and models.
        llm_requests: How many LLM steps the run took, where the backend
            reports it.
        stats: Optional token accounting -- input_tokens, output_tokens,
            cache_read_tokens, cost_usd.
    """
    stats = stats or {}
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           review_status = NULL, apply_backend = ?,
                           apply_llm_requests = ?, apply_input_tokens = ?,
                           apply_output_tokens = ?, apply_cache_read_tokens = ?,
                           apply_cost_usd = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, backend, llm_requests,
              stats.get("input_tokens"), stats.get("output_tokens"),
              stats.get("cache_read_tokens"), stats.get("cost_usd"), url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        review_status = _classify_review_status(error or "")
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           review_status = ?, apply_backend = ?,
                           apply_llm_requests = ?, apply_input_tokens = ?,
                           apply_output_tokens = ?, apply_cache_read_tokens = ?,
                           apply_cost_usd = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, review_status,
              backend, llm_requests, stats.get("input_tokens"),
              stats.get("output_tokens"), stats.get("cache_read_tokens"),
              stats.get("cost_usd"), url))
    conn.commit()


def _restore_status(job: dict) -> None:
    """Put a job back exactly as it was before acquire_job locked it.

    acquire_job overwrites apply_status with 'in_progress', so simply releasing
    the lock to NULL discards whatever the job's real outcome was. That matters
    for dry runs, which must leave no trace.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = ?, applied_at = ?, agent_id = NULL "
        "WHERE url = ? AND apply_status = 'in_progress'",
        (job.get("prior_status"), job.get("prior_applied_at"), job["url"]),
    )
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0,
               dry_run: bool = True) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Defaults to a dry-run prompt: --gen exists for manual debugging, and a
    # generated prompt gets piped into whatever harness is being tried, so it
    # must not tell that harness to submit a real application by default.
    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text,
                                     dry_run=dry_run)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    from applypilot.apply.backends.claude_code import _make_mcp_config
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            backend: str = "claude") -> tuple[str, int]:
    """Drive one job application through the selected backend.

    The backend owns everything about *how* the browser is driven; this layer
    stays agnostic so job acquisition, Chrome lifecycle, the dashboard and the
    retry/review classification are shared by all of them.

    Args:
        job: Job dict from the database.
        port: CDP port of this worker's Chrome.
        worker_id: Numeric worker identifier.
        model: Claude model name (ignored by backends that configure their
            own model, such as Goose).
        dry_run: Don't click the final Submit.
        backend: Backend name -- 'goose' or 'claude'.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    return get_backend(backend).run(
        job, port=port, worker_id=worker_id, model=model, dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------
# The vocabulary itself lives in `outcomes` so backends can share it without
# importing this module. Re-exported here because callers already import these
# names from `launcher`.

PERMANENT_FAILURES = outcomes.PERMANENT_FAILURES
DISQUALIFIED_REASONS = outcomes.DISQUALIFIED_REASONS
NEEDS_REVIEW_REASONS = outcomes.NEEDS_REVIEW_REASONS
PERMANENT_PREFIXES = outcomes.PERMANENT_PREFIXES

_classify_review_status = outcomes.classify_review_status
_is_permanent_failure = outcomes.is_permanent_failure


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                backend: str = "goose",
                fallback_backend: str | None = None,
                queue_batch: str | None = None) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.
        backend: Primary apply backend name -- 'goose' or 'claude'.
        fallback_backend: Backend to retry a job on when the primary one gives
            up for a driver-side reason. None disables the retry.
        queue_batch: Drain this user-selected batch instead of the ranked
            queue. See acquire_job.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    attempted: set[str] = set()  # this session only -- see acquire_job docstring
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     company_tier=None, last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, exclude_urls=attempted,
                          queue_batch=queue_batch)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0
        attempted.add(job["url"])

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                          model=model, dry_run=dry_run,
                                          backend=backend)
            run_stats = get_backend(backend).pop_run_stats(worker_id)
            used_backend = backend

            # Second chance on the fallback backend. Only for failures that
            # mean the *driver* gave up (outcomes.should_fall_back) -- a job
            # that is expired, already applied to, or behind an SSO wall is
            # just as dead for the stronger model, and retrying it would burn
            # Claude quota for nothing.
            if (not dry_run
                    and fallback_backend
                    and fallback_backend != backend
                    and not _stop_event.is_set()
                    and outcomes.should_fall_back(result)):
                first_reason = result.split(":", 1)[-1]
                add_event(f"[W{worker_id}] {backend} gave up ({first_reason[:20]}), "
                          f"retrying on {fallback_backend}")
                # Give the retry a clean browser. Goose may have left the page
                # mid-form, and the fallback prompt assumes a fresh start.
                if chrome_proc:
                    cleanup_worker(worker_id, chrome_proc)
                chrome_proc = launch_chrome(worker_id, port=port, headless=headless)
                result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                              model=model, dry_run=dry_run,
                                              backend=fallback_backend)
                run_stats = get_backend(fallback_backend).pop_run_stats(worker_id)
                used_backend = fallback_backend

            llm_requests = run_stats.get("llm_requests")

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif dry_run:
                # A dry run must not change the job's state. The agent is told
                # to report RESULT:APPLIED with a dry-run note, so without this
                # the job would be recorded as submitted and never picked up
                # again -- an application silently lost.
                # Restore the status the job had before we locked it. Releasing
                # to NULL would erase a real prior outcome -- dry-running a job
                # already marked 'applied' would delete the record of having
                # applied to it.
                _restore_status(job)
                outcome = result.split(":", 1)[0]
                add_event(f"[W{worker_id}] DRY RUN ({outcome}), not recorded: {job['title'][:28]}")
                update_state(worker_id, status="done",
                             last_action=f"dry run: {outcome} (not saved)")
                jobs_done += 1
                if target_url:
                    break
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms,
                            backend=used_backend, llm_requests=llm_requests,
                            stats=run_stats)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms, backend=used_backend,
                            llm_requests=llm_requests, stats=run_stats)
                if reason == "grad_date_mismatch":
                    _clear_terminal_flags_on_grad_date_mismatch(job["url"])
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         backend: str = "goose",
         fallback_backend: str | None = None,
         queue_batch: str | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        backend: Primary apply backend -- 'goose' (Goose CLI on a cheap
            OpenRouter model) or 'claude' (Claude Code CLI).
        fallback_backend: Backend to retry a job on when the primary one gives
            up for a driver-side reason. None disables the retry.
        queue_batch: Apply only to the jobs the user selected under this batch
            id, in the order they were selected. Set by the web UI; ignores
            min_score and every other ranked-queue gate.
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    # Mirror live progress to disk only when something is watching. A plain
    # terminal run writes no file and behaves exactly as it always has.
    if queue_batch:
        begin_run(batch=queue_batch, backend=backend, dry_run=dry_run)

    config.ensure_dirs()
    console = Console()

    # Resolve the backend up front: a missing dependency or unset API key
    # should surface before Chrome launches and a job is locked, not after.
    from rich.markup import escape  # backend hints contain [brackets] Rich would eat
    try:
        get_backend(backend).preflight()
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Cannot use --backend {backend}:[/red]\n{escape(str(exc))}")
        raise SystemExit(1)

    # The fallback is a nice-to-have, so a missing one is a warning rather than
    # a hard stop -- but it is checked here, not on the first job that needs it,
    # so the run doesn't discover the problem an hour in.
    if fallback_backend and fallback_backend != backend:
        try:
            get_backend(fallback_backend).preflight()
        except (ValueError, RuntimeError) as exc:
            console.print(
                f"[yellow]Fallback backend {fallback_backend!r} unavailable, "
                f"continuing without it:[/yellow]\n{escape(str(exc))}"
            )
            fallback_backend = None

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(
        f"Launching apply pipeline ({mode_label}, {worker_label}, "
        f"backend={backend}, poll every {POLL_INTERVAL}s)..."
    )
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Abort in-flight backend runs to skip the current jobs
            interrupt_all_backends()
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            interrupt_all_backends()
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    backend=backend,
                    fallback_backend=fallback_backend,
                    queue_batch=queue_batch,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            backend=backend,
                            fallback_backend=fallback_backend,
                            queue_batch=queue_batch,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
        # Stamp the run finished however it ended -- completed, Ctrl+C, or an
        # exception on the way out. A watcher that only ever saw "running"
        # cannot tell a crash from a long job.
        end_run()
