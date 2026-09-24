"""Attempt history, infra-failure classification, and the harness hash."""
import subprocess
from pathlib import Path

from applypilot.apply import launcher, outcomes, prompt
from applypilot.database import init_db


def test_record_run_appends_one_row_per_attempt(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "t.db")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: conn)
    job = {"url": "https://jobs.example/1"}
    launcher.record_run(job, 3, "2026-09-23T00:00:00+00:00", "failed:captcha", 1000,
                        "goose", False, {"model": "m", "llm_requests": 40, "cost_usd": 0.03})
    launcher.record_run(job, 5, "2026-09-23T01:00:00+00:00", "applied", 2000, "goose", False)
    rows = conn.execute("SELECT worker, outcome, reason, model, harness FROM apply_runs "
                        "ORDER BY id").fetchall()
    assert [tuple(r)[:4] for r in rows] == [(3, "failed", "captcha", "m"), (5, "applied", None, None)]
    assert rows[0]["harness"] == prompt.harness_version()


def test_infra_failures_are_browser_problems_only():
    for r in ("browser_down", "browser_runtime_unreachable", "no_browser_tools",
              "browser_transport_closed", "Browser_Unavailable"):
        assert outcomes.is_infra_failure(r), r
    for r in ("captcha", "page_error", "no_result_line", "expired", ""):
        assert not outcomes.is_infra_failure(r), r


def test_harness_hash_matches_deploy_script():
    """prompt.harness_version() and scripts/deploy_to_vm.sh must hash the same
    files in the same order, or traces and the deploy log won't line up."""
    apply_dir = Path(prompt.__file__).parent
    files = [str(apply_dir / f) for f in prompt._HARNESS_FILES]
    script = (Path(__file__).parent.parent / "scripts" / "deploy_to_vm.sh").read_text()
    for f in prompt._HARNESS_FILES:
        assert f'$APPLY_DIR/{f}"' in script, f
    shell = subprocess.run(f"cat {' '.join(files)} | shasum | cut -c1-12", shell=True,
                           capture_output=True, text=True).stdout.strip()
    assert shell == prompt.harness_version()
