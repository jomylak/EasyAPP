"""Live dashboard for the apply pipeline.

Displays real-time worker status, job progress, and recent events in a terminal
dashboard using the Rich library, and mirrors the same state to a JSON file so
a separate process -- the web server -- can render it too.

This module is the one place every backend reports progress through, which is
what makes that mirror possible without either backend knowing about it.
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    """Tracks the current state of the apply worker."""

    worker_id: int = 0
    status: str = "starting"  # starting, applying, applied, failed, expired, captcha, idle, done
    job_title: str = ""
    company: str = ""
    # 'tier1' | 'adjacent' | None -- see config.TIER1_COMPANIES. Lets the web
    # UI give the same gold treatment to a live worker's company name that it
    # gives one sitting in a table row.
    company_tier: str | None = None
    score: int = 0
    start_time: float = 0.0
    actions: int = 0
    last_action: str = ""
    jobs_applied: int = 0
    jobs_failed: int = 0
    jobs_done: int = 0
    total_cost: float = 0.0
    # Cumulative across every job this worker has run this session -- like
    # total_cost, these only land once per job (the backends' own streaming
    # protocols report token usage at job completion, not progressively
    # mid-job), so "live" here means "as fresh as the last finished job",
    # not a running counter within the job in progress.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    log_file: Path | None = None
    # The job's URL, so a consumer can join a live worker back to its table
    # row. The terminal dashboard has no use for it; the web UI does.
    url: str = ""
    # Rolling window of this worker's own recent tool-call descriptions
    # (timestamped), independent of the run-wide `_events` log below -- the
    # web UI's per-job expansion shows a single worker's stream, not every
    # worker interleaved. Capped at MAX_WORKER_ACTIONS the same way `_events`
    # is capped, for the same reason (a browser only needs it fresh, not
    # complete).
    recent_actions: list[str] = field(default_factory=list)
    # What this worker's Chrome is currently egressing through, e.g.
    # "static (America/Los_Angeles)", "home (...)", "direct" -- see
    # chrome.get_worker_proxy_label.
    proxy_label: str = ""
    # Cumulative captcha walls this worker has hit this session, on whatever
    # proxy it's currently assigned -- a rising count on one worker while
    # others stay flat is the signal that its static IP is degrading and
    # worth burning a manual replacement on.
    captcha_hits: int = 0


# Module-level state (thread-safe via _lock)
_worker_states: dict[int, WorkerState] = {}
_events: list[str] = []
_lock = threading.Lock()
MAX_EVENTS = 8
MAX_WORKER_ACTIONS = 10

# Run-level facts the web UI needs but no single worker owns. Set once by the
# launcher via begin_run(); left empty when nothing has started a run.
_run: dict = {}

# Publishing is debounced: worker updates arrive once or twice a second per
# worker and the file only has to be fresh enough for a browser to poll.
_PUBLISH_INTERVAL = 0.5
_last_publish = 0.0


# ---------------------------------------------------------------------------
# Publishing live state for out-of-process readers
# ---------------------------------------------------------------------------

# Events are stored with Rich markup because the terminal dashboard renders
# them. A browser should not have to know that, so it is stripped on the way
# out rather than stored twice.
_RICH_TAG = re.compile(r"\[/?[a-z][a-z0-9 _.#-]*\]")


def _snapshot() -> dict:
    """Build the JSON payload. Caller must hold _lock."""
    return {
        **_run,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "workers": [
            {**asdict(s), "log_file": str(s.log_file) if s.log_file else None}
            for s in sorted(_worker_states.values(), key=lambda w: w.worker_id)
        ],
        "events": [_RICH_TAG.sub("", e).strip() for e in _events],
        "totals": {
            "applied": sum(s.jobs_applied for s in _worker_states.values()),
            "failed": sum(s.jobs_failed for s in _worker_states.values()),
            "cost": round(sum(s.total_cost for s in _worker_states.values()), 4),
        },
    }


def _publish(force: bool = False) -> None:
    """Mirror current state to RUN_STATE_PATH. Caller must hold _lock.

    Written to a temp file and renamed, so a reader polling the path either
    sees the previous complete snapshot or the next one, never a half-written
    file. Failures are swallowed: a broken mirror must never take down an
    apply run that is otherwise working.
    """
    global _last_publish
    if not _run:
        return  # nothing has called begin_run(); this is a plain CLI run
    now = time.monotonic()
    if not force and now - _last_publish < _PUBLISH_INTERVAL:
        return
    _last_publish = now

    path = config.RUN_STATE_PATH
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_snapshot(), indent=1))
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("Could not publish run state: %s", exc)


def begin_run(batch: str | None = None, backend: str = "",
              dry_run: bool = False) -> None:
    """Start mirroring this run to disk. No-op for runs nobody is watching.

    The launcher calls this only when the run came from the web UI, so an
    ordinary `applypilot apply` on the terminal writes no file and behaves
    exactly as it did before.
    """
    with _lock:
        _run.clear()
        _run.update({
            "pid": os.getpid(),
            "batch": batch,
            "backend": backend,
            "dry_run": dry_run,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        })
        _publish(force=True)


def end_run() -> None:
    """Mark the run finished and leave a final snapshot behind.

    The file is kept rather than deleted so the UI can show how a run ended
    instead of the run simply vanishing; a reader tells live from finished by
    the finished_at field, not by the file's existence.
    """
    with _lock:
        if not _run:
            return
        _run["finished_at"] = datetime.now(timezone.utc).isoformat()
        _publish(force=True)


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

def init_worker(worker_id: int = 0) -> None:
    """Register the worker in the dashboard state."""
    with _lock:
        _worker_states[worker_id] = WorkerState(worker_id=worker_id)
        _publish()


def update_state(worker_id: int = 0, **kwargs) -> None:
    """Update the worker's state fields.

    Args:
        worker_id: Which worker to update.
        **kwargs: Field names and values to set on WorkerState.
    """
    with _lock:
        state = _worker_states.get(worker_id)
        if state is not None:
            for key, value in kwargs.items():
                setattr(state, key, value)
        _publish()


def accumulate_usage(worker_id: int, cost: float, stats: dict) -> None:
    """Add one turn's cost/token usage onto the worker's running totals."""
    with _lock:
        state = _worker_states.get(worker_id)
        if state is None:
            return
        state.total_cost += cost
        state.input_tokens += stats.get("input_tokens") or 0
        state.output_tokens += stats.get("output_tokens") or 0
        state.cache_read_tokens += stats.get("cache_read") or 0
        _publish()


def add_worker_action(worker_id: int, desc: str) -> None:
    """Append a timestamped tool-call description to a worker's own log.

    Kept separate from `update_state(last_action=...)`, which callers still
    call for the single-line summary the table row shows -- this feeds the
    web UI's expanded per-job view instead, which wants the last few steps,
    not just the latest one.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        state = _worker_states.get(worker_id)
        if state is not None:
            state.recent_actions.append(f"{ts} {desc}")
            if len(state.recent_actions) > MAX_WORKER_ACTIONS:
                state.recent_actions.pop(0)
        _publish()


def get_state(worker_id: int = 0) -> WorkerState | None:
    """Read the worker's current state."""
    with _lock:
        return _worker_states.get(worker_id)


def add_event(msg: str) -> None:
    """Add a timestamped event to the scrolling event log.

    Args:
        msg: Rich markup string describing the event.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _events.append(f"[dim]{ts}[/dim] {msg}")
        if len(_events) > MAX_EVENTS:
            _events.pop(0)
        _publish()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Status -> Rich style mapping
_STATUS_STYLES: dict[str, str] = {
    "starting": "dim",
    "idle": "dim",
    "applying": "yellow",
    "applied": "bold green",
    "failed": "red",
    "expired": "dim red",
    "captcha": "magenta",
    "login_issue": "red",
    "done": "bold",
}


def render_dashboard() -> Table:
    """Build the Rich table showing all worker statuses.

    Returns:
        A Rich Table object ready for display.
    """
    table = Table(title="ApplyPilot Dashboard", expand=True, show_lines=False)
    table.add_column("W", style="bold", width=3, justify="center")
    table.add_column("Job", min_width=30, max_width=50, no_wrap=True)
    table.add_column("Status", width=12, justify="center")
    table.add_column("Time", width=6, justify="right")
    table.add_column("Acts", width=5, justify="right")
    table.add_column("Last Action", min_width=20, max_width=35, no_wrap=True)
    table.add_column("Proxy", width=22, no_wrap=True)
    table.add_column("OK", width=4, justify="right", style="green")
    table.add_column("Fail", width=4, justify="right", style="red")
    table.add_column("Cost", width=8, justify="right")

    with _lock:
        states = sorted(_worker_states.values(), key=lambda s: s.worker_id)

    total_applied = 0
    total_failed = 0
    total_cost = 0.0

    for s in states:
        elapsed = ""
        if s.start_time and s.status == "applying":
            elapsed = f"{int(time.time() - s.start_time)}s"

        style = _STATUS_STYLES.get(s.status, "")
        status_text = Text(s.status.upper(), style=style)

        job_text = f"{s.job_title[:28]} @ {s.company[:16]}" if s.job_title else ""

        table.add_row(
            str(s.worker_id),
            job_text,
            status_text,
            elapsed,
            str(s.actions) if s.actions else "",
            s.last_action[:35] if s.last_action else "",
            f"{s.proxy_label[:20]} ({s.captcha_hits})" if s.captcha_hits else s.proxy_label[:22],
            str(s.jobs_applied),
            str(s.jobs_failed),
            f"${s.total_cost:.3f}" if s.total_cost else "",
        )
        total_applied += s.jobs_applied
        total_failed += s.jobs_failed
        total_cost += s.total_cost

    # Totals row
    table.add_section()
    table.add_row(
        "", "", "", "", "", "TOTAL", "",
        str(total_applied), str(total_failed), f"${total_cost:.3f}",
        style="bold",
    )

    return table


def render_full() -> Table | Group:
    """Render the dashboard table plus the recent events panel.

    Returns:
        A Rich Group (table + events panel) or just the table if no events.
    """
    table = render_dashboard()

    with _lock:
        event_lines = list(_events)

    if event_lines:
        event_text = Text.from_markup("\n".join(event_lines))
        events_panel = Panel(
            event_text,
            title="Recent Events",
            border_style="dim",
            height=min(MAX_EVENTS + 2, len(event_lines) + 2),
        )
        return Group(table, events_panel)

    return table


def get_totals() -> dict[str, int | float]:
    """Compute aggregate totals across all workers.

    Returns:
        Dict with keys: applied, failed, cost.
    """
    with _lock:
        applied = sum(s.jobs_applied for s in _worker_states.values())
        failed = sum(s.jobs_failed for s in _worker_states.values())
        cost = sum(s.total_cost for s in _worker_states.values())
    return {"applied": applied, "failed": failed, "cost": cost}
