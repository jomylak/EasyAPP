import importlib.util
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
