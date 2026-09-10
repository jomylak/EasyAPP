"""Local HTTP server for the ApplyPilot web UI.

Bound to 127.0.0.1 and nothing else, deliberately and permanently. This serves
tailored resume PDFs, full job descriptions, and the controls that spend money
on the user's behalf; the Chrome profiles it drives hold the user's logged-in
sessions. There is no --host option, so there is no way to widen it by
accident. The same rule was written into the fileserver this project used to
have: "Loopback only. Never bind 0.0.0.0 -- this serves personal documents."

The server never runs the apply pipeline in-process. `launcher.main()`
installs a global SIGINT handler and atexit hooks and expects to own the
process it runs in, so a run is spawned as a child `applypilot apply` and
watched through the JSON state file that apply/dashboard.py publishes.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from applypilot import config, costs
from applypilot.database import get_connection, init_db
from applypilot.web import queries

logger = logging.getLogger(__name__)

HOST = os.environ.get("APPLYPILOT_HOST", "127.0.0.1")
# Defaults to loopback-only, on purpose: this serves resumes and drives
# Chrome profiles holding logged-in sessions. APPLYPILOT_HOST exists only
# for binding to a private overlay-network interface (e.g. a Tailscale IP)
# on a headless deployment -- never set it to 0.0.0.0 or a public interface.
DEFAULT_PORT = 8420
STATIC_DIR = Path(__file__).parent / "static"

# The child `applypilot apply` process, when one is running. A single handle
# because one machine drives one set of Chrome workers -- a second concurrent
# run would fight the first for CDP ports.
_run_proc: subprocess.Popen | None = None


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

def read_run_state() -> dict | None:
    """The current run's published state, or None if there isn't one."""
    try:
        return json.loads(config.RUN_STATE_PATH.read_text())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)          # signal 0 tests for existence only
    except (OSError, ProcessLookupError):
        return False
    return True


def run_is_live() -> bool:
    state = read_run_state()
    return bool(state and not state.get("finished_at") and _pid_alive(state.get("pid")))


def reconcile_stale_locks() -> int:
    """Release apply locks left behind by a run that is no longer alive.

    `apply_status='in_progress'` is a lock with no heartbeat: if the launcher
    is killed, the rows it had claimed stay claimed forever and neither the
    ranked queue nor a batch will ever pick them up again. Nothing cleaned
    these up before, which is why the database currently has one.

    A row that belonged to a batch goes back to 'queued' so relaunching the
    batch retries it; one from the ranked queue goes back to NULL.

    Age-gated, not just keyed to run_is_live(): that check only sees runs
    this server itself spawned via /api/launch, so a run started straight
    from the CLI (a normal, supported way to run `applypilot apply`) has no
    PID here for it to find, and looked exactly like an orphan. The result:
    this function, called on every /api/stats poll, ripped the lock off a
    job that was still being actively applied to seconds after it was
    claimed, mid-run -- reproduced directly by launching `applypilot apply
    --url ...` over SSH while a dashboard tab had the UI open. A lock is
    only genuinely orphaned once it has sat untouched longer than any single
    job could legitimately still be running, regardless of how that run was
    launched -- so age is the real signal, and run_is_live() is kept only as
    a cheap short-circuit for the common case (a live web-launched run means
    nothing yet could be stale).
    """
    if run_is_live():
        return 0
    conn = get_connection()
    settings = config.load_settings()
    # Longest either backend's own per-job wall-clock cap allows, plus a
    # buffer -- a job that has run longer than this without updating
    # last_attempted_at is dead by definition, not just slow.
    max_age = max(
        settings.get("apply_timeout") or config.DEFAULTS["apply_timeout"],
        settings.get("goose_timeout") or config.DEFAULTS["goose_timeout"],
    ) + 300
    cur = conn.execute("""
        UPDATE jobs
           SET apply_status = CASE WHEN queue_batch IS NOT NULL
                                   THEN 'queued' ELSE NULL END,
               agent_id = NULL
         WHERE apply_status = 'in_progress'
           AND (last_attempted_at IS NULL
                OR last_attempted_at < datetime('now', '-' || ? || ' seconds'))
    """, (max_age,))
    conn.commit()
    if cur.rowcount:
        logger.info("Released %d stale apply lock(s).", cur.rowcount)
    return cur.rowcount


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Migrate and sweep before the first request rather than lazily, so a
    # database left mid-run by a killed launcher is already consistent by the
    # time the page loads.
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(reconcile_stale_locks)
    # Normalising stated pay is cheap to repeat: it only touches rows whose
    # pay has not been parsed yet, so every startup after the first is a no-op.
    from applypilot.pay import backfill as backfill_pay
    await asyncio.to_thread(lambda: backfill_pay(get_connection()))
    yield


app = FastAPI(title="ApplyPilot", docs_url=None, redoc_url=None,
              lifespan=_lifespan)


# --- reading ---------------------------------------------------------------

@app.get("/api/days")
def api_days() -> dict:
    return {"days": queries.list_days()}


@app.get("/api/jobs")
def api_jobs(
    day: str | None = None,
    sort: str = queries.DEFAULT_SORT,
    page: int = 0,
    page_size: int = 30,
    min_fit: int | None = None,
    min_desirability: float | None = None,
    min_prestige: int | None = None,
    min_pay: float | None = None,
    job_type: str | None = None,
    site: str | None = None,
    ats: str | None = None,
    q: str | None = None,
    above_pay_floor: bool = False,
    unapplied_only: bool = False,
    terminal_only: bool = False,
    likely_terminal_only: bool = False,
    eligible_only: bool = False,
    posted_within_days: int | None = None,
    tier_only: bool = False,
    include_tier: bool = False,
    location: str | None = None,
    term: str | None = None,
) -> dict:
    """One page of one day's table. Every day asks for its own independently.

    `ats` is comma-separated (e.g. "Workday,Greenhouse") -- the Browse tab's
    ATS filter is a checklist, not a single choice, and a comma-joined query
    param is simpler than a repeated-key list param on both ends for
    something that's just an OR over a handful of strings.

    Eligibility and the Spring/Summer-only internship term are enforced
    unconditionally inside queries._filter_clauses, not as opt-in flags here
    -- there's no reason this table should ever surface a job you can't
    honestly take or a term you can't work.

    `tier_only` narrows to big-tech postings; `include_tier` instead exempts
    them from the min_* bars, so a prestige-10 posting with a fit of 3 still
    shows up in a filtered view. `location` is one of nyc | metro | remote.
    """
    return queries.list_jobs(
        {
            "day": day, "min_fit": min_fit, "min_desirability": min_desirability,
            "min_prestige": min_prestige, "min_pay": min_pay,
            "job_type": job_type, "site": site,
            "ats": [a for a in ats.split(",") if a] if ats else None,
            "q": q,
            "above_pay_floor": above_pay_floor, "unapplied_only": unapplied_only,
            "terminal_only": terminal_only,
            "likely_terminal_only": likely_terminal_only,
            "eligible_only": eligible_only,
            "posted_within_days": posted_within_days,
            "tier_only": tier_only, "include_tier": include_tier,
            "location": location, "term": term,
        },
        sort=sort, page=page, page_size=page_size,
    )


@app.get("/api/job")
def api_job(url: str = Query(..., description="The job's URL (its primary key)")) -> dict:
    # A query parameter rather than a path segment: job URLs contain slashes
    # and colons, and round-tripping those through a path is a bug farm.
    job = queries.job_detail(url)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.get("/api/facets")
def api_facets() -> dict:
    return queries.facets()


@app.get("/api/stats")
def api_stats() -> dict:
    reconcile_stale_locks()
    return {"stats": queries.stats(), "run": read_run_state(), "live": run_is_live()}


@app.get("/api/applications")
def api_applications(status: str | None = None, limit: int = 200) -> dict:
    return {"rows": queries.applications(status=status, limit=limit)}


@app.get("/api/ats-stats")
def api_ats_stats() -> dict:
    # Dashboard-only filter to goose -- costs.ats_stats() itself stays
    # backend-inclusive since `applypilot ats-stats` and estimate_batch's
    # per-(ats, backend) sampling both still want Claude's historical rows.
    return {"rows": [r for r in costs.ats_stats() if r["backend"] == "goose"]}


@app.get("/api/company-limits")
def api_company_limits() -> dict:
    return {"rows": queries.company_limit_breakdown()}


@app.get("/api/resume")
def api_resume(url: str = Query(...)) -> FileResponse:
    """Serve the resume a job was applied with, as a PDF where one exists."""
    conn = get_connection()
    row = conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if not row or not row["tailored_resume_path"]:
        raise HTTPException(404, "No resume recorded for this job")

    txt = Path(row["tailored_resume_path"])
    path = txt.with_suffix(".pdf") if txt.with_suffix(".pdf").exists() else txt

    # The path came from our own database, but it is still a filesystem path
    # being turned into a download. Confine it to the resume directory so a
    # corrupted row cannot read arbitrary files.
    try:
        resolved = path.resolve()
        resolved.relative_to(config.TAILORED_DIR.resolve())
    except (ValueError, OSError):
        raise HTTPException(403, "Resume path is outside the resume directory")
    if not resolved.exists():
        raise HTTPException(404, "Resume file is missing from disk")

    return FileResponse(resolved, filename=resolved.name)


# --- the queue -------------------------------------------------------------

@app.post("/api/queue")
def api_queue(payload: dict = Body(...)) -> dict:
    """Mark the user's selection as queued and price it.

    Returns the batch id, which is what /api/launch runs. Selecting and
    launching are separate steps so the estimate can be shown and reconsidered
    before any money is spent.
    """
    urls = payload.get("urls") or []
    if not urls:
        raise HTTPException(400, "No jobs selected")

    backend = payload.get("backend") or config.load_settings().get("apply_backend", "goose")
    batch = f"batch-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    queued = 0
    with conn:
        for position, url in enumerate(urls):
            # Never re-queue something already applied or in flight; the UI
            # can be looking at a stale page.
            cur = conn.execute("""
                UPDATE jobs
                   SET apply_status = 'queued', queue_batch = ?,
                       queue_position = ?, queued_at = ?, apply_error = NULL
                 WHERE url = ?
                   AND (apply_status IS NULL OR apply_status IN ('failed', 'queued'))
            """, (batch, position, now, url))
            queued += cur.rowcount

    estimate = costs.estimate_batch(urls[:queued] or urls, backend, conn=conn)
    return {
        "batch": batch,
        "queued": queued,
        "skipped": len(urls) - queued,
        "estimate": estimate,
        "backend": backend,
    }


@app.post("/api/queue/estimate")
def api_estimate(payload: dict = Body(...)) -> dict:
    """Price a selection without committing it -- what the Launch bar shows."""
    urls = payload.get("urls") or []
    backend = payload.get("backend") or config.load_settings().get("apply_backend", "goose")
    return costs.estimate_batch(urls, backend)


@app.post("/api/queue/reorder")
def api_queue_reorder(payload: dict = Body(...)) -> dict:
    """Rewrite priority order for the given queued jobs (drag-to-reorder).

    Folds every url into one fresh batch, in the order given -- the queue is
    meant to read as a single prioritized list regardless of which browse
    session originally queued each job, and the apply launcher only drains
    one queue_batch at a time, so a reorder across batches has to merge them.
    Rows not still 'queued' (already picked up by a running worker) are
    silently skipped rather than erroring, since the drag happened against a
    snapshot that may be a few seconds stale.
    """
    urls = payload.get("urls") or []
    if not urls:
        raise HTTPException(400, "No jobs given")

    batch = f"batch-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    conn = get_connection()
    with conn:
        for position, url in enumerate(urls):
            conn.execute("""
                UPDATE jobs SET queue_batch = ?, queue_position = ?
                 WHERE url = ? AND apply_status = 'queued'
            """, (batch, position, url))
    return {"batch": batch}


@app.post("/api/unqueue")
def api_unqueue(payload: dict = Body(...)) -> dict:
    """Remove jobs from the queue before they start. Costs nothing.

    `revert_status`, if given, is what the row becomes instead of a clean
    NULL -- the one caller that uses this is the Applications tab's Cancel
    button, for a job that was queued by Retry rather than fresh. Reverting
    those to NULL would erase the fact that it ever failed, which reads as
    the job having been deleted rather than the retry having been cancelled.
    Restricted to 'failed' since that's the only status Retry queues from.
    """
    urls = payload.get("urls") or []
    if not urls:
        raise HTTPException(400, "No jobs given")
    revert_status = payload.get("revert_status")
    if revert_status is not None and revert_status != "failed":
        raise HTTPException(400, "revert_status must be 'failed' or omitted")

    conn = get_connection()
    placeholders = ",".join("?" * len(urls))
    with conn:
        cur = conn.execute(f"""
            UPDATE jobs
               SET apply_status = ?, queue_batch = NULL,
                   queue_position = NULL, queued_at = NULL
             WHERE url IN ({placeholders}) AND apply_status = 'queued'
        """, [revert_status, *urls])
    return {"removed": cur.rowcount}


# --- running ---------------------------------------------------------------

@app.post("/api/launch")
def api_launch(payload: dict = Body(...)) -> dict:
    """Start applying to a queued batch, in the background.

    Spawned as a child process rather than run here: launcher.main() installs
    a process-global SIGINT handler and atexit hooks, and would take the
    server down with it.
    """
    global _run_proc

    if run_is_live():
        raise HTTPException(409, "A run is already in progress")

    batch = payload.get("batch")
    if not batch:
        raise HTTPException(400, "No batch given")

    conn = get_connection()
    pending = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE queue_batch = ? AND apply_status = 'queued'",
        (batch,),
    ).fetchone()[0]
    if not pending:
        raise HTTPException(400, f"Batch {batch} has nothing waiting")

    cmd = [
        sys.executable, "-m", "applypilot.cli", "apply",
        "--queued", batch,
        "--workers", str(int(payload.get("workers", 1))),
        "--headless",
    ]
    if payload.get("backend"):
        cmd += ["--backend", str(payload["backend"])]
    if payload.get("dry_run"):
        cmd.append("--dry-run")

    log_path = config.LOG_DIR / f"web-run-{batch}.log"
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a")

    _run_proc = subprocess.Popen(
        cmd, stdout=log, stderr=subprocess.STDOUT,
        # Its own process group, so stopping the run takes the Chrome
        # processes it spawned with it instead of orphaning them.
        start_new_session=(os.name != "nt"),
    )
    logger.info("Launched batch %s as pid %d", batch, _run_proc.pid)
    return {"batch": batch, "pid": _run_proc.pid, "jobs": pending, "log": str(log_path)}


@app.post("/api/stop")
def api_stop(payload: dict = Body(...)) -> dict:
    """Stop one job.

    A queued job is simply unqueued -- instant, and it never cost anything.

    A job already in flight is stopped by killing its worker's Chrome. The
    backends already watch their CDP port and abort a run when it goes away,
    so this reuses a mechanism that exists rather than inventing a new IPC
    channel into the child process. It takes a few seconds to take effect,
    and the job is recorded as failed, which is what actually happened.
    """
    url = payload.get("url")
    if not url:
        raise HTTPException(400, "No url given")

    conn = get_connection()
    row = conn.execute("SELECT apply_status FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        raise HTTPException(404, "No such job")

    if row["apply_status"] == "queued":
        with conn:
            conn.execute(
                "UPDATE jobs SET apply_status = NULL, queue_batch = NULL,"
                " queue_position = NULL, queued_at = NULL WHERE url = ?", (url,))
        return {"action": "unqueued", "url": url}

    if row["apply_status"] != "in_progress":
        return {"action": "noop", "url": url, "status": row["apply_status"]}

    state = read_run_state() or {}
    worker = next((w for w in state.get("workers", []) if w.get("url") == url), None)
    if worker is None:
        raise HTTPException(409, "That job is marked in progress but no worker owns it")

    from applypilot.apply.chrome import BASE_CDP_PORT, _kill_on_port
    _kill_on_port(BASE_CDP_PORT + int(worker["worker_id"]))
    return {"action": "aborting", "url": url, "worker_id": worker["worker_id"],
            "note": "The worker notices its browser is gone within ~20s and "
                    "records the job as failed."}


@app.post("/api/stop-all")
def api_stop_all() -> dict:
    """Stop the whole run and release everything it had claimed."""
    global _run_proc

    state = read_run_state() or {}
    pid = state.get("pid")
    stopped = False
    if _pid_alive(pid):
        try:
            # The launcher's own SIGINT handler skips in-flight jobs on the
            # first press and shuts down on the second; going straight to the
            # process group is the equivalent of the second.
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            stopped = True
        except (OSError, ProcessLookupError) as exc:
            logger.warning("Could not signal run pid %s: %s", pid, exc)

    from applypilot.apply.chrome import kill_all_chrome
    kill_all_chrome()
    _run_proc = None
    released = reconcile_stale_locks()
    return {"stopped": stopped, "released": released}


# --- settings ----------------------------------------------------------

# Env vars editable from the Settings tab -- the ones the pipeline actually
# checks for at startup (config.py's has_llm / OPENROUTER_API_KEY checks),
# not an open-ended list. Kept short and explicit so a typo'd key from the
# UI can't silently write junk into .env.
_KNOWN_ENV_KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "LLM_URL", "APPLYPILOT_JOB_PASSWORD")


@app.get("/api/settings")
def api_get_settings() -> dict:
    return config.load_settings()


# Settings keys merged one level deep rather than replaced wholesale.
_NESTED_MERGE_KEYS = ("cost_defaults", "new_grad_weights", "internship_weights")


@app.post("/api/settings")
def api_update_settings(payload: dict = Body(...)) -> dict:
    """Merge the given keys into settings.json and save.

    A shallow merge, except `cost_defaults`, `new_grad_weights` and
    `internship_weights`, which are merged one level deeper: editing one
    backend's default cost or one weight doesn't silently drop the other
    entries in that dict.
    """
    current = config.load_settings()
    for key, value in payload.items():
        if key in _NESTED_MERGE_KEYS and isinstance(value, dict):
            current.setdefault(key, {}).update(value)
        else:
            current[key] = value
    config.save_settings(current)
    return current


@app.get("/api/env-keys")
def api_get_env_keys() -> dict:
    """Which known API keys are set -- never the values themselves.

    Read from the .env file directly rather than os.environ: os.environ was
    populated once at process start, so a key added after the server started
    wouldn't show as set until a restart if this read os.environ instead.
    """
    from dotenv import dotenv_values
    values = dotenv_values(config.ENV_PATH) if config.ENV_PATH.exists() else {}
    return {k: bool((values.get(k) or "").strip()) for k in _KNOWN_ENV_KEYS}


@app.post("/api/env-keys")
def api_set_env_keys(payload: dict = Body(...)) -> dict:
    """Write one or more API keys to ~/.applypilot/.env.

    An empty/missing value for a key means "leave it alone" -- there is no
    way to clear a key from this endpoint, only to set one, since the whole
    point of masked inputs on the frontend is that a blank box never means
    "I want to erase what's there."
    """
    from dotenv import set_key
    config.ensure_dirs()
    if not config.ENV_PATH.exists():
        config.ENV_PATH.touch()
    updated = []
    for key, value in payload.items():
        if key not in _KNOWN_ENV_KEYS:
            raise HTTPException(400, f"Unknown key: {key}")
        if not (value or "").strip():
            continue
        set_key(str(config.ENV_PATH), key, value.strip())
        updated.append(key)
    return {"updated": updated}


@app.get("/api/events")
async def api_events() -> StreamingResponse:
    """Server-sent events carrying the run state file as it changes.

    Only sends when something actually changed, so an idle page holds one
    quiet connection rather than a heartbeat of identical payloads.
    """
    async def stream():
        last = None
        while True:
            state = await asyncio.to_thread(read_run_state)
            payload = json.dumps({"run": state, "live": run_is_live()})
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --- the frontend ----------------------------------------------------------

if (STATIC_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
else:
    @app.get("/")
    def _no_build() -> dict:
        return {
            "error": "The frontend has not been built.",
            "fix": "cd web && npm install && npm run build",
            "api": "The /api/* endpoints work regardless.",
        }


def serve(port: int = DEFAULT_PORT, reload: bool = False) -> None:
    """Run the UI. Loopback only -- see the module docstring."""
    import uvicorn
    uvicorn.run("applypilot.web.server:app" if reload else app,
                host=HOST, port=port, reload=reload, log_level="warning")
