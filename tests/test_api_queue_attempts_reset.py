"""api_queue must reset apply_attempts on requeue.

mark_result stamps apply_attempts=99 as a "permanently dead" sentinel for any
permanent failure (precheck-expired, manual ATS, blocklist, a prior captcha-
backlog dead end). Manually requeuing a job that turned out to be a false
positive cleared apply_status/apply_error but left that 99 in place -- so the
next real failure of any kind (+1 -> 100) pushed it past every attempts<99
guard, including _select_captcha_backlog, with no retry and no visible error.
Reproduced live on 2026-09-22 with a precheck-false-positive job that hit a
genuine captcha on its very next attempt and silently never reached the
home-fallback worker.
"""

from applypilot.database import init_db
from applypilot.web import server


def test_requeue_resets_apply_attempts_below_captcha_backlog_gate(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "test.db")
    monkeypatch.setattr(server, "get_connection", lambda: conn)
    monkeypatch.setattr(server.costs, "estimate_batch", lambda *a, **k: {})

    url = "https://a.example/job"
    conn.execute(
        "INSERT INTO jobs (url, title, apply_status, apply_error, apply_attempts) "
        "VALUES (?, 'Some Job', 'failed', 'expired -- pre-check, http 403, no browser spend', 99)",
        (url,),
    )
    conn.commit()

    server.api_queue({"urls": [url]})

    row = conn.execute(
        "SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row["apply_status"] == "queued"
    assert row["apply_error"] is None
    assert row["apply_attempts"] == 0, (
        "a stale 99 here means the very next captcha/proxy_dropped failure "
        "pushes attempts to 100 and permanently excludes the job from "
        "_select_captcha_backlog's apply_attempts < 99 gate"
    )


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    from unittest import mock

    with tempfile.TemporaryDirectory() as d:
        conn = init_db(Path(d) / "test.db")
        with mock.patch.object(server, "get_connection", lambda: conn), \
             mock.patch.object(server.costs, "estimate_batch", lambda *a, **k: {}):
            url = "https://a.example/job"
            conn.execute(
                "INSERT INTO jobs (url, title, apply_status, apply_error, apply_attempts) "
                "VALUES (?, 'Some Job', 'failed', 'expired -- pre-check, http 403, no browser spend', 99)",
                (url,),
            )
            conn.commit()
            server.api_queue({"urls": [url]})
            row = conn.execute(
                "SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url = ?", (url,)
            ).fetchone()
            assert row["apply_status"] == "queued"
            assert row["apply_error"] is None
            assert row["apply_attempts"] == 0
    print("ok")
