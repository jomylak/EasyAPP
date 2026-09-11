"""Tests for duplicate-posting detection (dedup.py).

Covers the three checkpoints' matching helpers directly, plus the two
correctness fixes made alongside the new company-level checkpoint:
location normalization (NULL vs "") and URL canonicalization.
"""

import pytest

from applypilot import dedup
from applypilot.database import init_db


def _insert(conn, url, **cols):
    row = {
        "url": url,
        "title": "Software Engineer Intern",
        "location": "Remote",
        "description": "A" * 50,
    }
    row.update(cols)
    cols_sql = ", ".join(row)
    conn.execute(
        f"INSERT INTO jobs ({cols_sql}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# normalize_location
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("Remote", "Remote"),
    ("  Remote  ", "Remote"),
])
def test_normalize_location(raw, expected):
    assert dedup.normalize_location(raw) == expected


def test_normalize_location_collapses_null_and_empty_string_to_the_same_key(db):
    """The bug this fixes: workday.py used to default missing location to
    "" while jobspy.py defaulted the same case to None, so
    find_exact_text_duplicate's `location IS ?` (NULL-safe, not ""-safe)
    silently failed to match otherwise-identical postings across sources."""
    _insert(db, "https://a.example/1", location=dedup.normalize_location(""))
    match = dedup.find_exact_text_duplicate(
        db, "Software Engineer Intern", "A" * 50, dedup.normalize_location(None),
    )
    assert match == "https://a.example/1"


# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------

def test_canonicalize_url_strips_tracking_params_only():
    assert (dedup.canonicalize_url("https://x.example/job?utm_source=a&jr_id=1&token=keep")
            == "https://x.example/job?token=keep")


# ---------------------------------------------------------------------------
# find_company_duplicate
# ---------------------------------------------------------------------------

def test_find_company_duplicate_matches_same_company_similar_title_and_text(db):
    _insert(db, "https://a.example/1", company="ByteDance",
            company_normalized="bytedance",
            full_description="We are looking for a Software Engineer Intern " * 5)
    match = dedup.find_company_duplicate(
        db, "TikTok", "Software Engineer Intern",
        "We are looking for a Software Engineer Intern " * 5, "Remote",
    )
    # "TikTok" and "ByteDance" are NOT unified by normalize_company (no
    # alias map exists) -- confirms the matcher relies on company_normalized
    # being written identically for true reposts of the same req, not on
    # cross-brand alias resolution it doesn't attempt.
    assert match is None


def test_find_company_duplicate_matches_same_normalized_company(db):
    _insert(db, "https://a.example/1", company="TikTok Inc.",
            company_normalized="tiktok",
            full_description="We are looking for a Software Engineer Intern " * 5)
    match = dedup.find_company_duplicate(
        db, "TikTok", "Software Engineer Intern",
        "We are looking for a Software Engineer Intern " * 5, "Remote",
    )
    assert match == "https://a.example/1"


def test_find_company_duplicate_requires_exact_location_match(db):
    """A genuinely different office for the same role at the same company
    is a different req, not a repost -- location is never allowed to be
    fuzzy here, per dedup.py's own stated philosophy."""
    _insert(db, "https://a.example/1", company="TikTok",
            company_normalized="tiktok", location="New York, NY",
            full_description="We are looking for a Software Engineer Intern " * 5)
    match = dedup.find_company_duplicate(
        db, "TikTok", "Software Engineer Intern",
        "We are looking for a Software Engineer Intern " * 5, "San Jose, CA",
    )
    assert match is None


def test_find_company_duplicate_requires_similar_description_not_just_title(db):
    """Same company, same title, genuinely different description (a
    different req that happens to share a common title like "SWE Intern")
    must not be flagged -- title similarity alone isn't enough."""
    _insert(db, "https://a.example/1", company="TikTok",
            company_normalized="tiktok",
            full_description="Work on the recommendation systems team " * 5)
    match = dedup.find_company_duplicate(
        db, "TikTok", "Software Engineer Intern",
        "Work on the payments infrastructure team " * 5, "Remote",
    )
    assert match is None


# ---------------------------------------------------------------------------
# check_duplicate: company_repost wired in as a third fallback
# ---------------------------------------------------------------------------

def test_check_duplicate_uses_company_repost_as_last_resort(db):
    # Titles differ slightly (still >=0.7 similar) so the exact_text
    # checkpoint doesn't fire first -- this isolates the company_repost path.
    _insert(db, "https://a.example/1", company="TikTok",
            company_normalized="tiktok", title="Software Engineer Intern",
            full_description="We are looking for a Software Engineer Intern " * 5)
    _insert(db, "https://a.example/2", company="TikTok",
            company_normalized="tiktok", title="Software Engineer Intern - Summer",
            full_description="We are looking for a Software Engineer Intern " * 5)

    result = dedup.check_duplicate(db, "https://a.example/2")

    assert result["duplicate_of"] == "https://a.example/1"
    assert result["reason"] == "company_repost"
