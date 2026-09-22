import importlib.util
import os
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "orphan_reaper", Path(__file__).parent.parent / "scripts" / "orphan_reaper.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

CH = "/usr/bin/google-chrome --user-data-dir=/x/chrome-workers/worker-{}"


def test_orphans():
    rows = [
        (10, 10, 600, CH.format(1)),   # live worker -> keep
        (20, 20, 600, CH.format(2)),   # no row, past grace -> reap
        (30, 30, 60, CH.format(3)),    # no row, inside grace -> keep
        (40, 40, 600, CH.format(50)),  # fingerprint worker, inside its grace
        (50, 50, 600, "python other"),
    ]
    assert r.orphans(rows, {1}, True) == {2: (20, 600)}


def test_no_apply_process_means_all_orphaned():
    rows = [(10, 10, 600, CH.format(1))]
    assert r.orphans(rows, {1}, False) == {1: (10, 600)}


def test_stale_profile_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(r.config, "CHROME_WORKER_DIR", tmp_path)
    old, active, fresh = tmp_path / "worker-9", tmp_path / "worker-1", tmp_path / "worker-2"
    for d in (old, active, fresh):
        d.mkdir()
    stale_t = time.time() - r.STALE_DIR_S - 60
    os.utime(old, (stale_t, stale_t))
    os.utime(active, (stale_t, stale_t))  # old mtime, but Chrome still has it open

    rows = [(10, 10, 600, CH.format(1))]  # worker-1 has a live Chrome
    assert [d.name for d in r.stale_profile_dirs(rows)] == ["worker-9"]
