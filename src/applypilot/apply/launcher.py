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
import random
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
from applypilot.apply.failure_taxonomy import normalize_failure_reason
from applypilot.apply.ineligibility import sweep_company_siblings
from applypilot.apply.backends import get_backend, interrupt_all_backends
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome, get_worker_ip_info,
    cleanup_on_exit, BASE_CDP_PORT, get_worker_proxy_label,
)
from applypilot.apply import ip_health, network_stats, routing
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, render_full, get_totals,
    begin_run, end_run,
)
from applypilot.apply import humanizer
from applypilot.apply.expiry_check import check_listing_expired

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()
# Set once every primary worker has finished; lets the home-fallback worker
# exit after draining the captcha backlog those workers left behind, instead
# of being cut off (which _stop_event would do) or polling forever.
_primaries_done = threading.Event()
# Workers currently holding a claimed job. Added inside acquire_job before the
# claim commits, so a worker that finds the queue empty can never miss a peer
# that is mid-claim. A queued run stays alive while this is non-empty.
_busy: set[int] = set()
# How often an idle queued worker re-checks for new jobs while a peer is busy.
IDLE_POLL = 10

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
                  applied_at AS prior_applied_at, duplicate_of"""

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


def _company_locked_reason(conn, company: str | None, worker_id: int) -> str | None:
    """Whether another worker already has a job at this company in progress.

    Two workers racing to create an account on the same employer's ATS at the
    same time is exactly the kind of thing that produces a half-registered
    account or a password reset neither of them expects -- so only one worker
    may hold an in_progress row for a given company at a time. Scoped to
    *other* workers' rows only: a worker re-entering this check for its own
    in_progress row (there isn't one at claim time, but keeps this safe if
    that ever changes) must never lock itself out.
    """
    from applypilot import company_limits
    name = config.normalize_company(company)
    if not name:
        return None
    this_agent = f"worker-{worker_id}"
    rows = conn.execute(
        "SELECT company FROM jobs WHERE apply_status = 'in_progress' "
        "AND agent_id IS NOT NULL AND agent_id != ?",
        (this_agent,),
    ).fetchall()
    for r in rows:
        if company_limits._matches(config.normalize_company(r["company"]), name):
            return f"{company} already being applied to by another worker"
    return None


def _company_cap_reason(conn, company: str | None) -> str | None:
    """Whether `company` has already hit its per-period application cap (see
    company_limits.py) -- a blanket default for most employers, tighter
    confirmed caps for a few. Unlike the daily cap this resets on its own
    once the period rolls over, so a capped row is deferred for this run
    rather than given a terminal status."""
    from applypilot import company_limits
    status = company_limits.status_for(conn, company)
    if status["at_cap"]:
        period = "lifetime" if status["period"] == "total" else f"{status['period']}ly"
        return (f"{company} at its {period} cap "
                f"({status['applied']}/{status['limit']})")
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


def _live_duplicate_already_committed(conn, row) -> str | None:
    """URL of another row in this job's duplicate cluster that has already
    reached 'applied'/'manual'/'in_progress', or None.

    Duplicates share one visible row (dedup.link keeps the newest visible and
    points the older ones at it), so a repost is a new row for a job that may
    already have been applied to under the old one. Checked live at claim time
    rather than trusting stale status: only a member that has genuinely been
    submitted, or is being submitted right now, blocks the claim. Merely
    'related' rows (same company, similar title) never block anything.
    """
    from applypilot import dedup

    # A row queued before enrichment/scoring reached it has no links yet, so
    # link it now rather than trusting whatever duplicate_of said when it was read.
    dedup.link(conn, row["url"])
    cur = conn.execute("SELECT duplicate_of FROM jobs WHERE url = ?", (row["url"],)).fetchone()
    rep = (cur["duplicate_of"] if cur else None) or row["url"]
    hit = conn.execute(
        "SELECT url FROM jobs WHERE url != ? AND (url = ? OR duplicate_of = ?) "
        "AND apply_status IN ('applied', 'manual', 'in_progress') LIMIT 1",
        (row["url"], rep, rep),
    ).fetchone()
    return hit["url"] if hit else None


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


def _select_queued(conn, deferred: set, limit: int = 1):
    """Pick the next job off the manual queue -- every batch the web UI has
    ever queued, merged into one FIFO backlog.

    Deliberately applies none of the ranked branch's gates -- not the fit
    threshold, not the pay floor, not eligibility, not age decay. A human
    looked at these rows and chose them, so their selection *is* the ranking,
    and re-filtering it here would silently drop jobs they explicitly picked.

    Ordered by queued_at first, not just queue_position: position only
    resets to 0 within a single /api/queue call, so ordering on it alone
    would replay every batch's start at once instead of draining batches in
    the order they were queued.

    Confirmed duplicates (duplicate_of) are still selected here rather than
    excluded in SQL -- acquire_job turns them into a terminal 'failed' status
    right after selection (same pattern as the manual-ATS/blocked-site
    checks below), instead of leaving them stuck in 'queued' forever with no
    row this query would ever return to let anyone clear them.
    """
    params: list = []
    skip_clause = ""
    if deferred:
        skip_clause = f"AND url NOT IN ({','.join('?' * len(deferred))})"
        params.extend(sorted(deferred))
    cur = conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE apply_status = 'queued'
          {skip_clause}
        ORDER BY queued_at, queue_position, url
        LIMIT ?
    """, params + [limit])
    return cur.fetchone() if limit == 1 else cur.fetchall()


def _select_ranked(conn, min_score: int, skip: set,
                   blocked_sites: list, blocked_patterns: list, limit: int = 1):
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
    cur = conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed')
          -- A captcha hit on this row's primary static-proxy attempt is
          -- waiting for the dedicated home-fallback worker (see
          -- _select_captcha_backlog), not another primary worker on a
          -- different static IP -- exclude it here so it can't be
          -- double-claimed from both queues.
          AND (apply_error IS NULL OR apply_error != 'captcha')
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
        LIMIT ?
    """, [_settings.get("max_apply_attempts") or config.DEFAULTS["max_apply_attempts"]] + params
         + [config.DEFAULTS["job_age_decay_per_day"], limit])
    return cur.fetchone() if limit == 1 else cur.fetchall()


def _select_captcha_backlog(conn, deferred: set):
    """Jobs whose primary-tier static proxy hit a captcha wall -- or had its
    proxy connection drop mid-run -- waiting for the dedicated home-fallback
    worker's retry (see worker_loop's home_fallback lane). mark_result leaves
    these non-permanent (apply_attempts below the 99 sentinel) specifically
    so this query can find them -- _select_ranked excludes them so a
    different primary worker never grabs one first. If this retry also hits
    a captcha or a dropped connection, it's marked permanent
    (apply_attempts=99) and drops out of both queues for good.

    Both reasons share one backlog/one drain worker rather than getting
    separate lanes -- proxy_dropped is rare enough on its own (a rotating
    residential IP timing out mid-session) that a dedicated tier for it
    would sit idle almost always; the home-IP worker is already the "clean
    connection, one retry" escape hatch either way.
    """
    skip_clause = ""
    params: list = []
    if deferred:
        skip_clause = f"AND url NOT IN ({','.join('?' * len(deferred))})"
        params.extend(sorted(deferred))
    return conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE apply_status = 'failed'
          AND apply_error IN ('captcha', 'proxy_dropped')
          AND apply_attempts < 99
          {skip_clause}
        ORDER BY COALESCE(last_attempted_at, applied_at)
        LIMIT 1
    """, params).fetchone()


def _route(conn, rows, worker_id: int):
    """Drop rows another worker's company lock or a company cap rules out,
    then let routing.pick prefer an ATS this worker's IP isn't already on.
    Filtering first means the window isn't wasted on rows the checks in
    acquire_job would only defer. Those checks still run on the pick."""
    usable = [r for r in rows
              if not _company_locked_reason(conn, r["company"], worker_id)
              and not _company_cap_reason(conn, r["company"])]
    return routing.pick(conn, usable, worker_id) or (rows[0] if rows else None)


def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0,
                exclude_urls: set[str] | None = None,
                manual_queue: bool = False,
                home_fallback: bool = False) -> dict | None:
    """Atomically acquire the next job to apply to.

    Four ways to choose a row, one way to claim it. The claim is a
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
        manual_queue: Drain the manual queue (every batch the web UI has
            queued, FIFO) instead of the ranked queue, ignoring the ranked
            mode's gates entirely.
        home_fallback: This is the dedicated home-fallback worker -- draw
            exclusively from the captcha backlog (_select_captcha_backlog)
            instead of target_url/manual_queue/ranked selection.

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

            if home_fallback:
                row = _select_captcha_backlog(conn, deferred)
            elif target_url:
                row = _select_target(conn, target_url)
            elif manual_queue:
                row = _route(conn, _select_queued(
                    conn, deferred, routing.ROUTING_WINDOW), worker_id)
            else:
                row = _route(conn, _select_ranked(
                    conn, min_score, set(exclude_urls or ()) | deferred,
                    blocked_sites, blocked_patterns, routing.ROUTING_WINDOW),
                    worker_id)

            if not row:
                conn.rollback()
                return None

            # 'applied' must be a true terminal state -- nothing past this
            # point may ever reclassify it. This bit a real job: a row that
            # had genuinely succeeded (confirmed submission, real cost/turns
            # recorded) was later re-selected via --url after an async dedup
            # backfill set its duplicate_of, and the duplicate-of gate a few
            # lines below unconditionally overwrote it to
            # failed:duplicate_of -- silently erasing the record of a real
            # application (and leaving the actual duplicate sibling, which
            # was never applied to, free to be picked up and double-submit
            # to the same company later). _select_target is the only path
            # permissive enough to return an 'applied' row at all (it only
            # excludes 'in_progress'); guarding here protects every caller.
            if row["prior_status"] == "applied":
                conn.rollback()
                logger.warning(
                    "Refusing to re-process already-applied job: %s", row["url"][:80])
                if target_url:
                    return None
                deferred.add(row["url"])
                continue

            # A company cap only applies to rows this call picked on its own
            # -- an explicit --url is the user overriding the queue by hand,
            # and second-guessing that pick isn't this check's job.
            if not target_url:
                cap_reason = _company_cap_reason(conn, row["company"])
                if cap_reason:
                    conn.rollback()
                    logger.info("Skipping (company cap): %s -- %s",
                               row["url"][:80], cap_reason)
                    deferred.add(row["url"])
                    continue

                locked_reason = _company_locked_reason(conn, row["company"], worker_id)
                if locked_reason:
                    conn.rollback()
                    logger.info("Skipping (company locked): %s -- %s",
                               row["url"][:80], locked_reason)
                    deferred.add(row["url"])
                    continue

            apply_url = row["application_url"] or row["url"]
            now = datetime.now(timezone.utc).isoformat()

            # Two ways a row can turn out unusable once it is in hand. Both get
            # a terminal status rather than a silent skip, so a user-selected
            # batch always drains and the dashboard can say why a job never
            # ran. The ranked branch filters blocked sites in SQL already, so
            # that check only ever fires for a queued or targeted row.
            from applypilot.config import is_manual_ats
            duplicate_sibling_status = None
            if row["duplicate_of"]:
                sibling = conn.execute(
                    "SELECT apply_status FROM jobs WHERE url = ?", (row["duplicate_of"],),
                ).fetchone()
                duplicate_sibling_status = sibling["apply_status"] if sibling else None
            if row["duplicate_of"] and duplicate_sibling_status in ("applied", "in_progress"):
                # Only reachable via manual_queue/target_url: the ranked
                # branch's fit_gate_sql() already excludes duplicate_of rows
                # from selection. A human can still queue one from the web UI
                # (Browse hides confirmed duplicates, but the flag can be set
                # by the enrichment backfill after the row was already
                # queued), so this is the last check before money is spent
                # applying to a posting under two different URLs.
                #
                # Gated on the sibling's own status, not merely on
                # duplicate_of being set: checkpoint-time dedup marks
                # duplicate_of from content similarity alone, independent of
                # apply history, so a row can be "the duplicate" of a sibling
                # that itself was never applied to. Failing this row
                # unconditionally in that case doesn't prevent a double
                # apply (the sibling hasn't been applied to yet either) --
                # it just means the whole cluster loses its only chance to
                # be tried, and previously this could even overwrite a row
                # that HAD already been genuinely applied to (see the
                # already-applied guard above this loop).
                outcome = ("failed", f"duplicate_of:{row['duplicate_of']}")
            elif (live_dupe := _live_duplicate_already_committed(conn, row)):
                # duplicate_of was unset on this row -- either it was never
                # enriched/scored yet (the gap window between discovery and
                # checkpoint 2/3), or it genuinely isn't a duplicate of
                # anything at those checkpoints' confidence bar. Catch the
                # narrow case that matters here: a sibling row this content
                # actually matches has ALREADY been applied to/claimed/queued
                # -- not merely "looks similar," since a merely-similar
                # pending row could be a legitimately different req and
                # wrongly failing it would be worse than the rare double
                # apply this guards against.
                outcome = ("failed", f"duplicate_of:{live_dupe}")
            elif is_manual_ats(apply_url):
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
                    "apply_error_category = ?, "
                    "agent_id = NULL, last_attempted_at = ? WHERE url = ?",
                    (status, reason, normalize_failure_reason(reason), now, row["url"]),
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
            _busy.add(worker_id)
            conn.commit()

            if not (home_fallback or target_url):
                routing.record_claim(worker_id, row)
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

TERMINAL_FALSE_POSITIVES_PATH = config.LOG_DIR / "terminal_false_positives.jsonl"


def _log_terminal_false_positive(job_url: str, note: str) -> None:
    """Record a job the pipeline believed was terminal that a real apply
    attempt just proved otherwise (a grad_date_mismatch).

    Durable, append-only, JSON-lines -- one record per occurrence, meant to
    accumulate until there's enough volume for a human (or a future pass) to
    spot a recurring phrase that keeps fooling the scorer. `note` is the
    agent's own one-line account of what the form actually required -- the
    real ground truth here, since it's what the ATS itself asked, not a
    re-read of the job description. `likely_sentences` is best-effort only
    -- see scoring.scorer.GRAD_EVIDENCE_SENTENCE_RE's docstring.
    """
    from applypilot.scoring.scorer import GRAD_EVIDENCE_SENTENCE_RE

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT company, title, full_description, is_terminal_internship, "
            "       is_terminal_internship_likely, terminal_source "
            "FROM jobs WHERE url = ?", (job_url,),
        ).fetchone()
        if not row:
            return
        was_flagged = row["is_terminal_internship"] == "yes" or row["is_terminal_internship_likely"] == "yes"
        if not was_flagged:
            # Not a false positive -- the pipeline never thought this one was
            # terminal, so there's nothing to learn from here.
            return
        sentences = GRAD_EVIDENCE_SENTENCE_RE.findall(row["full_description"] or "")
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "url": job_url,
            "company": row["company"],
            "title": row["title"],
            "was_confirmed": row["is_terminal_internship"] == "yes",
            "was_likely": row["is_terminal_internship_likely"] == "yes",
            "terminal_source": row["terminal_source"],
            "agent_note": note,
            "likely_sentences": [s.strip() for s in sentences][:5],
        }
        config.ensure_dirs()
        with open(TERMINAL_FALSE_POSITIVES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning("Could not log terminal false positive for %s: %s", job_url, e)


def _clear_terminal_flags_on_grad_date_mismatch(job_url: str, note: str = "") -> None:
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
    _log_terminal_false_positive(job_url, note)
    try:
        conn = get_connection()
        # requires_returning_student = 'yes' here too, not just cleared
        # terminal flags -- this is the single strongest evidence the
        # pipeline ever gets that this employer's program is enrollment-
        # gated (a live form actually rejected the candidate on it, not an
        # LLM read of the posting text), and without recording it here it
        # never counts toward compute_company_pattern_non_terminal()'s
        # internal-evidence threshold for THIS row -- only the sibling
        # postings below got that treatment, so the row that actually
        # proved it was undercounting its own company's pattern by exactly
        # the one data point that triggered this function in the first
        # place.
        conn.execute(
            "UPDATE jobs SET is_terminal_internship = 'no', "
            "is_terminal_internship_likely = 'no', "
            "requires_returning_student = 'yes' WHERE url = ?",
            (job_url,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT company, company_tier FROM jobs WHERE url = ?", (job_url,)
        ).fetchone()
        company = row["company"] if row else None
        if company:
            company_tier = row["company_tier"] if "company_tier" in row.keys() else None
            sibling_count = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE company = ? "
                "AND job_type = 'internship' AND url != ?",
                (company, job_url),
            ).fetchone()["n"]
            broad_employer = bool(company_tier) or sibling_count > _SIBLING_SWEEP_MAX
            note = (
                "Sibling posting at this company confirmed a graduation-date "
                "form mismatch (grad_date_mismatch)."
                if broad_employer else
                "Sibling posting at this company confirmed a graduation-date "
                "form mismatch (grad_date_mismatch) -- treating this posting "
                "as requiring the same continued enrollment."
            )
            sweep_company_siblings(job_url, company, company_tier, note, conn=conn)
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
    # Token/cost columns accumulate across attempts (COALESCE(col, 0) + new),
    # same as apply_attempts already does -- a retried job made a separate,
    # separately-billed OpenRouter call each time, and a plain overwrite here
    # was silently discarding every attempt but the last one's real spend.
    # That's real money OpenRouter charged that the dashboard never showed.
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, apply_error_category = NULL,
                           agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           review_status = NULL, apply_backend = ?,
                           apply_llm_requests = COALESCE(apply_llm_requests, 0) + ?,
                           apply_input_tokens = COALESCE(apply_input_tokens, 0) + ?,
                           apply_output_tokens = COALESCE(apply_output_tokens, 0) + ?,
                           apply_cache_read_tokens = COALESCE(apply_cache_read_tokens, 0) + ?,
                           apply_cost_usd = COALESCE(apply_cost_usd, 0) + ?
            WHERE url = ?
        """, (now, duration_ms, task_id, backend, llm_requests or 0,
              stats.get("input_tokens") or 0, stats.get("output_tokens") or 0,
              stats.get("cache_read_tokens") or 0, stats.get("cost_usd") or 0, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        error = error or "unknown"
        review_status = _classify_review_status(error)
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_error_category = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           review_status = ?, apply_backend = ?,
                           apply_llm_requests = COALESCE(apply_llm_requests, 0) + ?,
                           apply_input_tokens = COALESCE(apply_input_tokens, 0) + ?,
                           apply_output_tokens = COALESCE(apply_output_tokens, 0) + ?,
                           apply_cache_read_tokens = COALESCE(apply_cache_read_tokens, 0) + ?,
                           apply_cost_usd = COALESCE(apply_cost_usd, 0) + ?
            WHERE url = ?
        """, (status, error, normalize_failure_reason(error), duration_ms,
              task_id, review_status,
              backend, llm_requests or 0, stats.get("input_tokens") or 0,
              stats.get("output_tokens") or 0, stats.get("cache_read_tokens") or 0,
              stats.get("cost_usd") or 0, url))
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
                           apply_error = NULL, apply_error_category = NULL,
                           agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        reason = reason or "manual"
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_error_category = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason, normalize_failure_reason(reason), url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_error_category = NULL,
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
    network_stats.reset(port)
    t0 = time.time()
    status = "error"
    try:
        status, duration_ms = get_backend(backend).run(
            job, port=port, worker_id=worker_id, model=model, dry_run=dry_run,
        )
    finally:
        stats = network_stats.read(port)
        stats["elapsed_s"] = round(time.time() - t0, 1)
        _log_network_stats(job, worker_id, backend, dry_run, stats)
        ip_health.log_job(job, worker_id, status, dry_run, get_worker_ip_info(worker_id))
    return status, duration_ms


_NETWORK_STATS_LOG = Path("logs/network_stats.jsonl")


def _log_network_stats(job: dict, worker_id: int, backend: str,
                        dry_run: bool, stats: dict) -> None:
    """Append one apply run's network-usage breakdown as a JSONL line.

    Read-only telemetry, no schema migration -- see network_stats.py's
    docstring for why this costs nothing to collect.
    """
    try:
        _NETWORK_STATS_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "url": job.get("url"),
            "company": job.get("company"),
            "ats": job.get("ats") or job.get("site"),
            "worker_id": worker_id,
            "backend": backend,
            "dry_run": dry_run,
            **stats,
        }
        with _NETWORK_STATS_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
        mb = stats.get("total_bytes", 0) / 1_000_000
        add_event(
            f"worker {worker_id}: {mb:.1f}MB / {stats.get('requests', 0)} reqs "
            f"({stats.get('pages', 0)} pages)"
        )
    except Exception:
        logger.debug("network_stats: failed to log", exc_info=True)


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

def _relaunch_chrome(worker_id: int, port: int, headless: bool, humanizer_stop,
                      home_fallback: bool = False):
    """Launch (or relaunch) Chrome for a worker and restart its humanizer.

    Stops the previous humanizer thread first if one was already running, so
    it never keeps driving mouse/typing jitter against a browser that's been
    torn down.
    """
    if humanizer_stop is not None:
        humanizer.stop(humanizer_stop)
    chrome_proc = launch_chrome(worker_id, port=port, headless=headless,
                                home_fallback=home_fallback)
    update_state(worker_id, proxy_label=get_worker_proxy_label(worker_id))
    return chrome_proc, humanizer.start(port)


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                backend: str = "goose",
                manual_queue: bool = False,
                home_fallback: bool = False) -> tuple[int, int]:
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
        manual_queue: Drain the manual queue instead of the ranked queue. See
            acquire_job. Unlimited (limit=0 from main()). An idle worker
            stays alive, polling, for as long as any peer is holding a job, so
            jobs queued mid-run are picked up; once every worker is idle and
            the queue is empty the run ends and the next launch starts fresh.
        home_fallback: This is the dedicated home-fallback worker: launches
            Chrome through the shared home-IP relay instead of a static
            proxy, and only ever draws from the captcha backlog (see
            acquire_job/_select_captcha_backlog) rather than the normal
            queue. A captcha hit here is final -- there's no further tier
            to escalate to.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    captcha_hits = 0  # this worker's static proxy hitting a captcha wall -- see below
    attempted: set[str] = set()  # this session only -- see acquire_job docstring
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        _busy.discard(worker_id)  # back at the top of the loop = not holding a job
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     company_tier=None, last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, exclude_urls=attempted,
                          manual_queue=manual_queue, home_fallback=home_fallback)
        if not job:
            if manual_queue and (_busy - {worker_id}):
                # Peers are still working, so the run is still alive: stay
                # idle, and pick up anything queued in the meantime.
                update_state(worker_id, status="idle",
                             last_action=f"waiting for jobs ({len(_busy)} running)")
                if _stop_event.wait(timeout=IDLE_POLL):
                    break
                continue
            if not continuous or manual_queue or (
                    home_fallback and _primaries_done.is_set()):
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

        if not dry_run:
            # Catch a closed/expired listing with a plain page fetch before
            # a worker spends browser-agent tokens discovering the same
            # thing the hard way (see expiry_check.py). Skipped in dry runs,
            # which must leave no trace -- same reasoning as _restore_status.
            expiry_reason = check_listing_expired(job["application_url"] or job["url"])
            if expiry_reason:
                add_event(f"[W{worker_id}] Pre-check: expired -- {job['title'][:30]}")
                mark_result(job["url"], "failed", expiry_reason, permanent=True)
                update_state(worker_id, status="expired",
                             last_action="expired (pre-check, no browser spend)")
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
                jobs_done += 1
                if target_url:
                    break
                continue

        # Jitter before every job start, not just at pipeline boot -- several
        # idle workers all waking up on the same freshly-queued batch is the
        # same synchronized-burst shape as a cold boot, and produced real
        # OpenRouter "provider timed out" failures in a concurrency benchmark
        # (2026-09-18: 3 of 8 simultaneous first-turn calls to the same cheap
        # model timed out). Unconditional and per-job rather than trying to
        # detect "are other workers also idle right now" -- correct either
        # way, and 10-20s is nothing against jobs that run for minutes.
        if _stop_event.wait(timeout=random.uniform(10, 20)):
            break

        chrome_proc = None
        humanizer_stop = None
        if not home_fallback:
            try:  # re-validate this worker's proxy IP before every job
                from applypilot.apply import webshare
                webshare.before_job(worker_id, log=lambda m: add_event(
                    f"[W{worker_id}] proxy pool: {m}"))
            except Exception as e:
                logger.warning("proxy pool pre-job check failed: %s", e)

        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc, humanizer_stop = _relaunch_chrome(
                worker_id, port, headless, humanizer_stop, home_fallback=home_fallback)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                          model=model, dry_run=dry_run,
                                          backend=backend)
            run_stats = get_backend(backend).pop_run_stats(worker_id)
            used_backend = backend

            if result.split(":", 1)[-1].strip().lower() == "captcha":
                captcha_hits += 1
                update_state(worker_id, captcha_hits=captcha_hits)

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
                permanent = _is_permanent_failure(result)
                if reason.strip().lower() in ("captcha", "proxy_dropped") and not home_fallback:
                    # Leave this non-permanent -- the dedicated home-fallback
                    # worker still owes it one retry via _select_captcha_backlog
                    # (proxy_dropped rides the same backlog as captcha; see its
                    # docstring). Only that worker's own hit (home_fallback=True
                    # here) is the true dead end -- no further tier to escalate to.
                    permanent = False
                mark_result(job["url"], "failed", reason,
                            permanent=permanent,
                            duration_ms=duration_ms, backend=used_backend,
                            llm_requests=llm_requests, stats=run_stats)
                if (not home_fallback and normalize_failure_reason(reason)
                        in ip_health._BLOCK_CATEGORIES):
                    try:  # re-score this worker's IP; swap it if it went bad
                        from applypilot.apply import webshare
                        webshare.on_block(worker_id, log=lambda m: add_event(
                            f"[W{worker_id}] proxy pool: {m}"))
                    except Exception as e:
                        logger.warning("proxy pool re-check failed: %s", e)
                if reason.startswith("grad_date_mismatch"):
                    note = reason[len("grad_date_mismatch"):].lstrip(" -:").strip()
                    _clear_terminal_flags_on_grad_date_mismatch(job["url"], note)
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
            humanizer.stop(humanizer_stop)
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    _busy.discard(worker_id)
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         backend: str = "goose",
         manual_queue: bool = False) -> None:
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
        manual_queue: Drain every batch the web UI has queued, FIFO across
            batches, in the order each was selected within its own batch.
            Set by the web UI; ignores min_score and every other ranked-queue
            gate. The run ends once the queue is empty and every worker is
            idle (see worker_loop); jobs queued after that need a new launch.
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()
    _primaries_done.clear()

    # Mirror live progress to disk only when something is watching. A plain
    # terminal run writes no file and behaves exactly as it always has.
    if manual_queue:
        begin_run(batch="queue", backend=backend, dry_run=dry_run)

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

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Webshare pool: swap out any IP with a bad fraud score before workers
    # read APPLY_PROXY_<n>. Best-effort -- a failure keeps the current proxies.
    try:
        from applypilot.apply import webshare
        if webshare.enabled():
            webshare.ensure(log=lambda m: console.print(f"[dim]proxy pool: {m}[/dim]"))
    except Exception as e:
        console.print(f"[yellow]proxy pool check skipped: {e}[/yellow]")

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    # One dedicated worker permanently assigned to the home-IP relay,
    # draining the captcha backlog every other worker's static proxy leaves
    # behind (see worker_loop's home_fallback param). Only spun up when
    # multi-worker (a single-worker run is dev/testing, not the real
    # pipeline) and when there's actually a home relay configured to drain
    # into -- otherwise captcha hits just sit in the backlog unclaimed,
    # same as today when APPLY_PROXY is unset.
    home_worker_id = workers if (workers > 1 and config.apply_proxy_configured()) else None
    if home_worker_id is not None:
        init_worker(home_worker_id)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    if home_worker_id is not None:
        worker_label += " + 1 home-fallback"
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
                    manual_queue=manual_queue,
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

                pool_size = workers + (1 if home_worker_id is not None else 0)
                with ThreadPoolExecutor(max_workers=pool_size,
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
                            manual_queue=manual_queue,
                        ): i
                        for i in range(workers)
                    }
                    home_future = None
                    if home_worker_id is not None:
                        # Idle-polls an empty backlog while the primary workers
                        # (above) may still feed it captcha hits; once they're
                        # all done it drains what's left and exits
                        # (_primaries_done, set below).
                        home_future = executor.submit(
                            worker_loop,
                            worker_id=home_worker_id,
                            limit=0,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            backend=backend,
                            home_fallback=True,
                        )

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                    if home_future is not None:
                        # Primary workers are done -- nothing left to feed
                        # the backlog, so the home worker drains it and exits.
                        _primaries_done.set()
                        try:
                            home_result = home_future.result()
                        except Exception:
                            logger.exception("Home-fallback worker crashed")
                            home_result = (0, 0)
                        results.append(home_result)

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
