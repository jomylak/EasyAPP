"""Concurrent workers must never be handed the same job.

Regression for 2026-09-22: dedup.link() committed from inside acquire_job's
BEGIN IMMEDIATE claim transaction, releasing the write lock before the row was
flipped to in_progress. Workers blocked on the lock then re-selected the same
still-queued row -- five workers opened one D. E. Shaw posting at once, Roku
was submitted twice, and the orphan reaper killed the extras mid-run.
"""

import threading

from applypilot.apply import launcher
from applypilot.database import get_connection, init_db


def test_parallel_acquire_never_hands_out_the_same_row(tmp_path, monkeypatch):
    path = tmp_path / "race.db"
    conn = init_db(path)
    for i in range(20):
        conn.execute(
            "INSERT INTO jobs (url, title, site, application_url, company, fit_score, "
            "apply_status, queue_batch, queue_position, queued_at) "
            "VALUES (?, 'Engineer', 'test', ?, ?, 9, 'queued', 'b', ?, '2026-01-01')",
            (f"https://jobs.example/{i}", f"https://ats.example/{i}", f"Company {i}", i))
    conn.commit()
    # Each worker thread gets its own connection, exactly as in production.
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: get_connection(path))

    workers = 8
    # The race is timing-dependent; several rounds make a regression near-certain to show.
    for _ in range(6):
        conn.execute("UPDATE jobs SET apply_status = 'queued', agent_id = NULL")
        conn.commit()
        barrier = threading.Barrier(workers)
        got: list[str | None] = [None] * workers

        def claim(i):
            barrier.wait()
            job = launcher.acquire_job(worker_id=i, manual_queue=True)
            got[i] = job and job["url"]

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        claimed = [u for u in got if u]
        assert len(claimed) == workers
        assert len(set(claimed)) == workers, f"same row claimed twice: {sorted(claimed)}"


def test_later_failure_never_overwrites_applied(tmp_path, monkeypatch):
    """A killed/duplicate sibling reporting failure after the real success
    must leave the job 'applied' (Roku, 2026-09-22)."""
    conn = init_db(tmp_path / "t.db")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: conn)
    conn.execute("INSERT INTO jobs (url, title, site, apply_status) "
                 "VALUES ('https://jobs.example/r', 'Engineer', 'test', 'in_progress')")
    conn.commit()

    launcher.mark_result("https://jobs.example/r", "applied")
    launcher.mark_result("https://jobs.example/r", "failed", "page_error")

    row = conn.execute("SELECT apply_status, apply_error FROM jobs").fetchone()
    assert (row["apply_status"], row["apply_error"]) == ("applied", None)
