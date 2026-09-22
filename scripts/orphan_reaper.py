"""Kill Chrome/goose processes whose apply worker is gone, and un-stick the
in_progress rows they leave behind.

A worker's try/finally cleanup never runs on SIGKILL/OOM/reboot, so its Chrome
can keep hitting the paid proxy indefinitely, and its job stays in_progress
forever (nothing else recovers it). Run from a systemd timer every 2 min.

    python scripts/orphan_reaper.py [--dry-run]
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from applypilot import config
from applypilot.database import get_connection

GRACE_S = 180          # Chrome is briefly up between jobs with no row
EXEMPT_WORKER = 50     # fingerprint-check's worker id
EXEMPT_GRACE_S = 900
STALE_ROW_S = 50 * 60  # goose_timeout is 40 min
STALE_DIR_S = 60 * 60  # a profile dir untouched an hour with no Chrome on it
                        # is leftover from a one-off run (e.g. a past
                        # --workers N larger than the current 8), not a
                        # worker slot anything will reuse.
WORKER_RE = re.compile(r"chrome-workers/worker-(\d+)")
WORKER_DIR_RE = re.compile(r"^worker-(\d+)$")


def ps_rows() -> list[tuple[int, int, int, str]]:
    """(pid, pgid, age_s, cmdline) for every process."""
    out = subprocess.run(["ps", "-eo", "pid=,pgid=,etimes=,args="],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    return rows


def orphans(rows, live_workers: set[int], apply_running: bool) -> dict[int, tuple[int, int]]:
    """{worker_id: (pgid, oldest_age_s)} for Chrome past its grace period."""
    found: dict[int, tuple[int, int]] = {}
    for pid, pgid, age, cmd in rows:
        m = WORKER_RE.search(cmd)
        if not m or "chrome" not in cmd.lower():
            continue
        w = int(m.group(1))
        if apply_running and w in live_workers:
            continue
        if age <= (EXEMPT_GRACE_S if w == EXEMPT_WORKER else GRACE_S):
            continue
        found[w] = (pgid, max(age, found.get(w, (0, 0))[1]))
    return found


def stale_profile_dirs(rows) -> list:
    """Worker profile dirs with no Chrome on them and untouched for a while.

    62 of these had piled up on the VM (worker-9, worker-20..98, ...) from
    past runs with more workers than the current fixed 8 -- nothing was ever
    deleting them, just trim_chrome_caches.sh clearing their inner caches.
    """
    active = {int(m.group(1)) for _pid, _pgid, _age, cmd in rows
              if (m := WORKER_RE.search(cmd)) and "chrome" in cmd.lower()}
    now = time.time()
    stale = []
    if not config.CHROME_WORKER_DIR.exists():
        return stale
    for d in config.CHROME_WORKER_DIR.iterdir():
        m = WORKER_DIR_RE.match(d.name)
        if not d.is_dir() or not m or int(m.group(1)) in active:
            continue
        try:
            if now - d.stat().st_mtime > STALE_DIR_S:
                stale.append(d)
        except OSError:
            continue
    return stale


def kill_group(pgid: int) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(5)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = ps_rows()
    apply_running = any("applypilot apply" in c or "applypilot.cli apply" in c
                        for _, _, _, c in rows if "orphan_reaper" not in c)
    conn = get_connection()
    live = {int(r[0].split("-")[1]) for r in conn.execute(
        "SELECT agent_id FROM jobs WHERE apply_status='in_progress' "
        "AND agent_id LIKE 'worker-%'")}

    for w, (pgid, age) in orphans(rows, live, apply_running).items():
        print(f"reap worker-{w} chrome pgid={pgid} age={age}s dry_run={args.dry_run}")
        if not args.dry_run:
            kill_group(pgid)

    for d in stale_profile_dirs(rows):
        print(f"remove stale profile dir {d} dry_run={args.dry_run}")
        if not args.dry_run:
            shutil.rmtree(d, ignore_errors=True)

    if not apply_running:
        # last_attempted_at is written as UTC isoformat, so compare like with like.
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_ROW_S)).isoformat()
        cur = conn.execute(
            "SELECT url FROM jobs WHERE apply_status='in_progress' AND "
            "last_attempted_at < ?", (cutoff,))
        urls = [r[0] for r in cur]
        for u in urls:
            print(f"recover stale in_progress row {u} dry_run={args.dry_run}")
        if urls and not args.dry_run:
            conn.executemany(
                "UPDATE jobs SET apply_status='failed', agent_id=NULL, "
                "apply_error='failed:orphaned', "
                "apply_attempts=COALESCE(apply_attempts,0)+1 WHERE url=?",
                [(u,) for u in urls])
            conn.commit()


if __name__ == "__main__":
    main()
