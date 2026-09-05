"""Tests for user-selected apply batches.

The web UI lets a person tick jobs and launch exactly those, which is a
different contract from the ranked queue: the selection *is* the ranking, so
none of the ranked branch's gates may apply to it. These cases pin that
contract, plus the deferral loop that keeps one unusable row from stranding
every job behind it in a batch.
"""

import pytest

from applypilot.apply import launcher
from applypilot.database import init_db


def _insert(conn, url, **cols):
    """Insert a job with sane defaults, overriding whatever the test cares about."""
    row = {
        "url": url,
        "title": "Software Engineer Intern",
        "site": "Intern List - SWE",
        "application_url": url,
        "tailored_resume_path": "/tmp/resume.txt",
        "fit_score": 9,
        "apply_status": "queued",
        "queue_batch": "batch-1",
        "queue_position": 0,
    }
    row.update(cols)
    cols_sql = ", ".join(row)
    conn.execute(
        f"INSERT INTO jobs ({cols_sql}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real, empty SQLite schema, with launcher pointed at it."""
    conn = init_db(tmp_path / "test.db")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: conn)
    return conn


# ---------------------------------------------------------------------------
# _select_queued: order and the absence of gates
# ---------------------------------------------------------------------------

def test_queued_drains_in_user_order(db):
    """queue_position wins, not fit_score -- the user chose the order."""
    _insert(db, "https://a.example/1", queue_position=2, fit_score=10)
    _insert(db, "https://a.example/2", queue_position=0, fit_score=3)
    _insert(db, "https://a.example/3", queue_position=1, fit_score=7)

    row = launcher._select_queued(db, "batch-1", set())
    assert row["url"] == "https://a.example/2"


def test_queued_ignores_other_batches(db):
    _insert(db, "https://a.example/1", queue_batch="batch-2")
    assert launcher._select_queued(db, "batch-1", set()) is None


def test_queued_skips_deferred_rows(db):
    _insert(db, "https://a.example/1", queue_position=0)
    _insert(db, "https://a.example/2", queue_position=1)

    row = launcher._select_queued(db, "batch-1", {"https://a.example/1"})
    assert row["url"] == "https://a.example/2"


@pytest.mark.parametrize("gate", [
    # Every one of these would exclude the row from the ranked queue. A human
    # picked it anyway, and silently dropping their pick is the failure mode
    # this test exists to prevent.
    {"fit_score": 2},
    {"pay_below_floor": "yes"},
    {"eligible": "no"},
    {"apply_attempts": 99},
    {"desirability_score": 0.0},
])
def test_queued_ignores_every_ranked_gate(db, gate):
    _insert(db, "https://a.example/1", **gate)
    row = launcher._select_queued(db, "batch-1", set())
    assert row is not None, f"a user-selected job was dropped by {gate}"


def test_queued_only_takes_rows_still_marked_queued(db):
    """A row already claimed or finished is not handed out again."""
    _insert(db, "https://a.example/1", apply_status="in_progress")
    assert launcher._select_queued(db, "batch-1", set()) is None


# ---------------------------------------------------------------------------
# blocked-site matching, shared between the SQL and Python paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern,expected", [
    ("%glassdoor%", "glassdoor"),
    ("%google.com/about/careers%", "google.com/about/careers"),
    ("%workopolis.com/out%", "workopolis.com/out"),
])
def test_like_patterns_become_substrings(pattern, expected):
    assert launcher._like_to_substring(pattern) == expected


@pytest.mark.parametrize("site,url,blocked", [
    ("Glassdoor", "https://example.com/j/1", True),          # by site name
    ("Intern List - SWE", "https://glassdoor.com/j/1", True),  # by url pattern
    ("Intern List - SWE", "https://GLASSDOOR.com/j/1", True),  # case-insensitive
    ("Intern List - SWE", "https://greenhouse.io/j/1", False),
])
def test_is_blocked(site, url, blocked):
    assert launcher._is_blocked(site, url, ["Glassdoor"], ["%glassdoor%"]) is blocked


# ---------------------------------------------------------------------------
# acquire_job: claiming, and the deferral loop
# ---------------------------------------------------------------------------

def test_acquire_claims_the_row(db):
    _insert(db, "https://a.example/1")
    job = launcher.acquire_job(queue_batch="batch-1", worker_id=3)

    assert job["url"] == "https://a.example/1"
    status, agent = db.execute(
        "SELECT apply_status, agent_id FROM jobs WHERE url = ?",
        ("https://a.example/1",)).fetchone()
    assert status == "in_progress"
    assert agent == "worker-3"


def test_unusable_row_does_not_strand_the_rest_of_the_batch(db):
    """The regression this loop exists for.

    acquire_job used to return None when it hit a manual-ATS row, and
    worker_loop reads None as "queue empty" and stops. A single such row
    partway down a batch therefore stranded every job behind it as 'queued'
    forever.
    """
    _insert(db, "https://ibegin.tcsapps.com/x", queue_position=0,
            application_url="https://ibegin.tcsapps.com/x")
    _insert(db, "https://a.example/2", queue_position=1)

    job = launcher.acquire_job(queue_batch="batch-1")

    assert job is not None, "batch stalled on the first unusable row"
    assert job["url"] == "https://a.example/2"

    # and the skipped row is recorded, not left dangling as 'queued'
    status, err = db.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url LIKE '%ibegin%'"
    ).fetchone()
    assert status == "manual"
    assert err == "manual ATS"


def test_blocked_row_is_failed_not_silently_skipped(db):
    """A queued blocked site gets a terminal outcome so the batch can finish."""
    _insert(db, "https://www.glassdoor.com/job/1", queue_position=0,
            application_url="https://www.glassdoor.com/job/1")
    _insert(db, "https://a.example/2", queue_position=1)

    job = launcher.acquire_job(queue_batch="batch-1")

    assert job["url"] == "https://a.example/2"
    status, err = db.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url LIKE '%glassdoor%'"
    ).fetchone()
    assert status == "failed"
    assert err == "site_blocked"


def test_empty_batch_returns_none(db):
    assert launcher.acquire_job(queue_batch="batch-1") is None


def test_targeted_unusable_url_returns_none_instead_of_looping(db):
    """--url on a manual-ATS job must not spin: there is no other row to move to."""
    _insert(db, "https://ibegin.tcsapps.com/x", apply_status=None,
            application_url="https://ibegin.tcsapps.com/x")
    assert launcher.acquire_job(target_url="https://ibegin.tcsapps.com/x") is None
