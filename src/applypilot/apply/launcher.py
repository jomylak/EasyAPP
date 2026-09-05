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
import re
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

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0,
                exclude_urls: set[str] | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        exclude_urls: URLs already attempted in this session. Needed because a
            dry run deliberately leaves the job's status untouched, so without
            this the same top-scoring job is handed back every iteration.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path, keywords,
                       apply_status AS prior_status, applied_at AS prior_applied_at
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            _settings = config.load_settings()
            # Build parameterized filters to avoid SQL injection
            from applypilot.database import fit_gate_sql
            fit_gate, params = fit_gate_sql(min_score)
            seen_clause = ""
            if exclude_urls:
                placeholders = ",".join("?" * len(exclude_urls))
                seen_clause = f"AND url NOT IN ({placeholders})"
                params.extend(sorted(exclude_urls))
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path, keywords,
                       apply_status AS prior_status, applied_at AS prior_applied_at
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
                -- Terminal internships -- ones that don't require returning
                -- to school, functionally a new-grad bridge role -- are rare
                -- (a few dozen out of thousands) and have already cleared
                -- every gate above (pay floor, eligibility) by the time we
                -- get here, so there's no reason to hold one back waiting
                -- for something hypothetically better: sort on the flag
                -- FIRST, ahead of the score blend entirely, so one always
                -- wins when it exists.
                --
                -- Below that: rank on a weighted blend of skill match and
                -- desirability, then decay by age, then a small blanket edge
                -- for new_grad roles generally -- internships consistently
                -- outnumber new_grad roles in the high-score tiers here (see
                -- `applypilot status`), so a plain highest-score-wins order
                -- would apply to a lot more internships than new-grad roles
                -- by volume alone, not because they're actually better
                -- matches. Jobs scored before desirability existed fall back
                -- to their fit_score so they still order sanely.
                ORDER BY
                  (CASE WHEN is_terminal_internship = 'yes' THEN 1 ELSE 0 END) DESC,
                  (fit_score * ? + COALESCE(desirability_score, fit_score) * ?)
                  - (julianday('now') - julianday(COALESCE(posted_date, discovered_at))) * ?
                  + (CASE WHEN job_type = 'new_grad' THEN 0.5 ELSE 0 END) DESC,
                  COALESCE(posted_date, discovered_at) DESC,
                  url
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params
                 + [_settings.get("fit_weight", 0.5),
                    _settings.get("desirability_weight", 0.5),
                    config.DEFAULTS["job_age_decay_per_day"]]).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
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


def _flip_grad_year(variant: str) -> str | None:
    """Return the same resume track with the other graduation year.

    The failure this serves is specifically a grad_date_mismatch, so the year
    is the only axis that should move -- the track was chosen from the job's
    own title and keywords and is still correct. Back when there were exactly
    two variants, "the other one" happened to mean this; with six it does not,
    and picking an arbitrary other variant could answer a returning-student
    posting with a May-2027 resume, which is the mismatch this is meant to fix.
    """
    # Rows tailored before tracks existed carry the old two-variant names.
    # Map them onto the current scheme so they keep swapping as they used to
    # instead of silently becoming un-swappable.
    legacy = {"default": "swe_2027", "returning_2028": "swe_2028"}
    variant = legacy.get(variant, variant)

    if "_" not in variant:
        return None
    track, _, year = variant.rpartition("_")
    other = {"2027": "2028", "2028": "2027"}.get(year)
    return f"{track}_{other}" if other else None


def swap_resume_variant_for_retry(job_url: str, title: str, site: str) -> None:
    """After a grad_date_mismatch failure, switch to the same resume track
    with the other graduation year so the next retry (this failure isn't in
    PERMANENT_FAILURES, so a retry is already permitted) uses a resume that
    actually matches what the form required, instead of repeating the exact
    same mismatch up to max_apply_attempts times.

    Best-effort and silent on failure -- a job that can't be swapped just
    retries with its current resume, no worse off than before this existed.
    """
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT resume_variant FROM jobs WHERE url = ?", (job_url,)
        ).fetchone()
        settings = config.load_settings()
        current_variant = (row["resume_variant"] if row else None) or \
            settings.get("default_resume_variant", "default")

        variants = settings.get("resume_variants", {})
        new_variant = _flip_grad_year(current_variant)
        if not new_variant or new_variant not in variants:
            logger.warning("No opposite-grad-year variant for %r; not swapping.",
                           current_variant)
            return

        txt_path, pdf_path, _grad_date, _start_date = config.get_resume_variant_paths(new_variant)
        if not txt_path.exists() or not pdf_path.exists():
            logger.warning("Can't swap to resume variant '%s': files missing", new_variant)
            return

        safe_title = re.sub(r"[^\w\s-]", "", title or "")[:50].strip().replace(" ", "_")
        safe_site = re.sub(r"[^\w\s-]", "", site or "")[:20].strip().replace(" ", "_")
        prefix = f"{safe_site}_{safe_title}"
        dest_txt = config.TAILORED_DIR / f"{prefix}.txt"
        dest_pdf = config.TAILORED_DIR / f"{prefix}.pdf"
        dest_txt.write_bytes(txt_path.read_bytes())
        dest_pdf.write_bytes(pdf_path.read_bytes())

        # A grad_date_mismatch is only ever discovered here because the form
        # itself required a graduation window scoring judged this candidate
        # already satisfied without returning to school -- i.e. the job
        # turned out not to be a "terminal" internship after all, even if it
        # was flagged as one. Clear the flag so it drops back to being
        # ranked on plain fit+desirability like any other internship,
        # instead of keeping a guaranteed-top-priority sort that its own
        # apply attempt just proved wrong.
        conn.execute(
            "UPDATE jobs SET resume_variant = ?, tailored_resume_path = ?, "
            "is_terminal_internship = 'no' WHERE url = ?",
            (new_variant, str(dest_txt), job_url),
        )
        conn.commit()
        logger.info("grad_date_mismatch: swapped resume variant %s -> %s for %s "
                     "(cleared is_terminal_internship if it was set)",
                     current_variant, new_variant, title)
    except Exception as e:
        logger.warning("Could not swap resume variant after grad_date_mismatch: %s", e)


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
                fallback_backend: str | None = None) -> tuple[int, int]:
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
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, exclude_urls=attempted)
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
                    swap_resume_variant_for_retry(job["url"], job["title"], job.get("site", ""))
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
         fallback_backend: str | None = None) -> None:
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
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

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
