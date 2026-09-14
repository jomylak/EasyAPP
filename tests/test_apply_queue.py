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


def test_duplicate_row_is_failed_when_sibling_already_applied(db):
    """A confirmed duplicate (dedup.check_duplicate) must never be applied
    to when its sibling has genuinely already been submitted -- even in
    queue_batch mode where the ranked branch's gates are deliberately
    skipped, since this one isn't a ranking gate, it's "don't apply to the
    same posting twice under two URLs." A canonical row not flagged as a
    duplicate of anything is unaffected and still gets claimed."""
    _insert(db, "https://a.example/1", apply_status="applied")
    _insert(db, "https://a.example/2", queue_position=0,
            duplicate_of="https://a.example/1")
    _insert(db, "https://a.example/3", queue_position=1)

    job = launcher.acquire_job(queue_batch="batch-1")

    assert job["url"] == "https://a.example/3", "batch stalled on the duplicate row"
    status, err = db.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = 'https://a.example/2'"
    ).fetchone()
    assert status == "failed"
    assert err == "duplicate_of:https://a.example/1"


def test_duplicate_row_is_still_tried_when_sibling_never_applied(db):
    """duplicate_of is set by checkpoint-time content matching alone,
    independent of apply history -- a row can be "the duplicate" of a
    sibling that was itself never applied to (neither side has been tried
    yet). Failing this row unconditionally in that case doesn't prevent any
    double apply (the sibling hasn't been applied to either) and just wastes
    the whole cluster's only chance to be tried, so it must still be
    claimed normally."""
    _insert(db, "https://a.example/1", apply_status="queued", queue_batch="other-batch")
    _insert(db, "https://a.example/2", queue_position=0,
            duplicate_of="https://a.example/1")

    job = launcher.acquire_job(queue_batch="batch-1")

    assert job["url"] == "https://a.example/2"


def test_already_applied_row_is_never_reprocessed(db):
    """An 'applied' row must be a true terminal state: even if it is later
    re-selected (e.g. --url on a job whose duplicate_of got backfilled after
    it had already succeeded), acquire_job must refuse to touch it rather
    than let a downstream gate overwrite a real, confirmed application."""
    _insert(db, "https://a.example/1", apply_status="applied",
            duplicate_of="https://a.example/9", apply_cost_usd=0.20)

    job = launcher.acquire_job(target_url="https://a.example/1")

    assert job is None
    status, cost = db.execute(
        "SELECT apply_status, apply_cost_usd FROM jobs WHERE url = 'https://a.example/1'"
    ).fetchone()
    assert status == "applied"
    assert cost == 0.20


def test_gap_window_duplicate_is_failed_not_applied_to(db):
    """Two rows with identical company/title/description/location, neither
    with duplicate_of set (the gap window: enrichment/scoring hasn't run
    the backfill on either yet). One has already been applied to -- the
    live recheck must fail the second rather than double-applying, but
    only because a same-content sibling has *already been committed*, not
    merely because it looks similar."""
    _insert(db, "https://a.example/1", queue_position=0,
            apply_status="applied",
            company="TikTok", full_description="A" * 50, location="Remote")
    _insert(db, "https://a.example/2", queue_position=1,
            company="TikTok", full_description="A" * 50, location="Remote")

    job = launcher.acquire_job(queue_batch="batch-1")

    assert job is None, "the only queued row was the gap-window duplicate"
    status, err = db.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = 'https://a.example/2'"
    ).fetchone()
    assert status == "failed"
    assert err == "duplicate_of:https://a.example/1"


def test_empty_batch_returns_none(db):
    assert launcher.acquire_job(queue_batch="batch-1") is None


def test_targeted_unusable_url_returns_none_instead_of_looping(db):
    """--url on a manual-ATS job must not spin: there is no other row to move to."""
    _insert(db, "https://ibegin.tcsapps.com/x", apply_status=None,
            application_url="https://ibegin.tcsapps.com/x")
    assert launcher.acquire_job(target_url="https://ibegin.tcsapps.com/x") is None


# ---------------------------------------------------------------------------
# Daily caps: soft stops on acquiring new jobs, not on in-flight ones
# ---------------------------------------------------------------------------

def test_no_caps_configured_is_a_noop(db):
    assert launcher._daily_cap_reason(db, {}) is None
    assert launcher._daily_cap_reason(db, {"max_daily_spend_usd": None, "max_daily_applications": None}) is None


def test_daily_spend_cap_reached(db):
    _insert(db, "https://a.example/1", apply_status="applied", apply_cost_usd=3.0,
            last_attempted_at="2026-09-05T10:00:00")
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    db.execute("UPDATE jobs SET last_attempted_at = ? WHERE url = 'https://a.example/1'", (today,))
    db.commit()

    reason = launcher._daily_cap_reason(db, {"max_daily_spend_usd": 2.0})
    assert reason is not None and "spend" in reason
    assert launcher._daily_cap_reason(db, {"max_daily_spend_usd": 5.0}) is None


def test_daily_application_cap_reached(db):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    _insert(db, "https://a.example/1", apply_status="applied", last_attempted_at=today)
    _insert(db, "https://a.example/2", apply_status="failed", last_attempted_at=today)
    db.commit()

    reason = launcher._daily_cap_reason(db, {"max_daily_applications": 2})
    assert reason is not None and "application" in reason
    assert launcher._daily_cap_reason(db, {"max_daily_applications": 5}) is None


def test_queued_row_does_not_count_toward_daily_cap(db):
    """Only terminal outcomes (applied/failed) count -- a job sitting in the
    queue hasn't spent anything and shouldn't itself trip the cap."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    _insert(db, "https://a.example/1", apply_status="queued", queued_at=today)
    db.commit()
    assert launcher._daily_cap_reason(db, {"max_daily_applications": 1}) is None


def test_acquire_job_stops_once_daily_cap_hit(db, monkeypatch):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    _insert(db, "https://a.example/already-applied", apply_status="applied", last_attempted_at=today)
    _insert(db, "https://a.example/2", apply_status="queued")
    db.commit()

    monkeypatch.setattr(launcher.config, "load_settings", lambda: {"max_daily_applications": 1})
    assert launcher.acquire_job(queue_batch="batch-1") is None

    # Confirm it really was the cap, not an empty queue: raise the cap and the
    # same still-queued row is claimable again.
    monkeypatch.setattr(launcher.config, "load_settings", lambda: {"max_daily_applications": 5})
    job = launcher.acquire_job(queue_batch="batch-1")
    assert job["url"] == "https://a.example/2"


# ---------------------------------------------------------------------------
# Per-company caps: some employers won't consider more than N in a period
# ---------------------------------------------------------------------------

def test_company_cap_reason_is_none_below_the_limit(db):
    _insert(db, "https://a.example/1", company="Wayne Enterprises",
            apply_status="applied", applied_at="2026-09-01T10:00:00")
    assert launcher._company_cap_reason(db, "Wayne Enterprises") is None


def test_company_cap_reason_fires_at_the_blanket_default(db):
    """Six is the blanket default for any company with no confirmed number."""
    for i in range(6):
        _insert(db, f"https://a.example/{i}", company="Wayne Enterprises",
                apply_status="applied", applied_at="2026-09-01T10:00:00")
    reason = launcher._company_cap_reason(db, "Wayne Enterprises")
    assert reason is not None and "6/6" in reason


def test_company_cap_matches_name_variants(db):
    """'Google LLC' and 'Google' must be recognized as the same employer, or
    the cap never fires for the exact spelling a given posting happens to use."""
    for i in range(3):
        _insert(db, f"https://a.example/{i}", company="Google LLC",
                apply_status="applied", applied_at="2026-09-01T10:00:00")
    assert launcher._company_cap_reason(db, "Google") is not None


def test_company_cap_does_not_apply_to_a_targeted_url(db):
    """--url is the user overriding the queue by hand; the cap shouldn't
    second-guess an explicit pick."""
    for i in range(6):
        _insert(db, f"https://a.example/applied-{i}", company="Wayne Enterprises",
                apply_status="applied", applied_at="2026-09-01T10:00:00")
    _insert(db, "https://a.example/target", company="Wayne Enterprises",
            apply_status=None, tailored_resume_path="/tmp/resume.txt")

    job = launcher.acquire_job(target_url="https://a.example/target")
    assert job is not None
    assert job["url"] == "https://a.example/target"


def test_acquire_job_skips_a_queued_row_at_its_company_cap(db):
    for i in range(6):
        _insert(db, f"https://a.example/applied-{i}", company="Wayne Enterprises",
                apply_status="applied", applied_at="2026-09-01T10:00:00")
    _insert(db, "https://a.example/capped", company="Wayne Enterprises",
            queue_position=0)
    _insert(db, "https://a.example/ok", company="Other Co", queue_position=1)

    job = launcher.acquire_job(queue_batch="batch-1")
    assert job["url"] == "https://a.example/ok"
