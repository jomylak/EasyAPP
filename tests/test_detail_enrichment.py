"""Tests for the aggregator-boilerplate guard in enrichment/detail.py.

Jobright.ai's own JSON-LD occasionally carries a promotional blurb as the
JobPosting `description` field instead of the actual posting. It's short but
clears the old 50-char floor, so it used to get stored (and scored) as if it
were real content. These cases pin the rejection and the requeue path that
gets already-affected rows another chance.
"""

import pytest

from applypilot.database import init_db
from applypilot.enrichment import detail

BOILERPLATE = (
    "AI Tools\nCustomize Your Resume\nMaximize your interview chances\n"
    "Build Cover Letter\nMake your application stand out\n"
    "Analyze How Well You Fit\nUnderstand your strength & weakness"
)

REAL_DESCRIPTION = (
    "We are looking for a Software Engineer Intern to join our team. "
    "Responsibilities include writing Python, reviewing pull requests, "
    "and shipping features end to end. Requirements: CS degree in progress, "
    "familiarity with distributed systems, strong communication skills."
)


def test_boilerplate_is_detected():
    assert detail.is_aggregator_boilerplate(BOILERPLATE) is True


def test_real_description_is_not_flagged():
    assert detail.is_aggregator_boilerplate(REAL_DESCRIPTION) is False


def test_none_and_empty_are_not_flagged():
    assert detail.is_aggregator_boilerplate(None) is False
    assert detail.is_aggregator_boilerplate("") is False


def test_json_ld_rejects_boilerplate_description():
    intel = {
        "json_ld": [{
            "@type": "JobPosting",
            "description": BOILERPLATE,
            "url": "https://jobright.ai/jobs/info/abc",
        }],
    }
    assert detail.extract_from_json_ld(intel) is None


def test_json_ld_accepts_a_real_description():
    intel = {
        "json_ld": [{
            "@type": "JobPosting",
            "description": REAL_DESCRIPTION,
            "url": "https://boards.greenhouse.io/acme/jobs/1",
        }],
    }
    result = detail.extract_from_json_ld(intel)
    assert result is not None
    assert "Software Engineer Intern" in result["full_description"]


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


def _insert(conn, url, full_description, detail_scraped_at="2026-09-08T00:00:00"):
    conn.execute(
        "INSERT INTO jobs (url, full_description, detail_scraped_at, detail_attempts, "
        "detail_error) VALUES (?,?,?,?,?)",
        (url, full_description, detail_scraped_at, 1, None),
    )
    conn.commit()


def test_requeue_boilerplate_rows_resets_only_affected_rows(db):
    _insert(db, "https://a.example/boilerplate", BOILERPLATE)
    _insert(db, "https://a.example/real", REAL_DESCRIPTION)

    n = detail.requeue_boilerplate_rows(db)
    assert n == 1

    row = db.execute(
        "SELECT detail_scraped_at, full_description, detail_attempts "
        "FROM jobs WHERE url = 'https://a.example/boilerplate'"
    ).fetchone()
    assert row["detail_scraped_at"] is None
    assert row["full_description"] is None
    assert row["detail_attempts"] == 0

    untouched = db.execute(
        "SELECT detail_scraped_at, full_description FROM jobs WHERE url = 'https://a.example/real'"
    ).fetchone()
    assert untouched["detail_scraped_at"] == "2026-09-08T00:00:00"
    assert untouched["full_description"] == REAL_DESCRIPTION


def test_requeue_boilerplate_rows_is_a_noop_when_nothing_matches(db):
    _insert(db, "https://a.example/real", REAL_DESCRIPTION)
    assert detail.requeue_boilerplate_rows(db) == 0
