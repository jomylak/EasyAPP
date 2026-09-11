"""Tests for per-company application caps.

Some employers cap how many applications they'll actually consider from one
candidate in a period; company_limits.py is what decides whether a given
company is still under its cap, and what the Dashboard tab shows for it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from applypilot import company_limits, config
from applypilot.database import init_db


def _insert(conn, url, company, applied_at):
    conn.execute(
        "INSERT INTO jobs (url, company, apply_status, applied_at) VALUES (?,?,?,?)",
        (url, company, "applied", applied_at),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


def test_unknown_company_gets_the_blanket_default(monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {})
    assert company_limits.get_limit("Some Random Startup") == (company_limits.DEFAULT_LIMIT, "total")


def test_known_company_gets_its_confirmed_limit(monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {})
    assert company_limits.get_limit("Google LLC") == (3, "month")
    assert company_limits.get_limit("TikTok (ByteDance)") == (2, "season")


def test_settings_json_can_override_known_limits(monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {
        "company_application_limits": {"google": {"limit": 1, "period": "month"}},
    })
    assert company_limits.get_limit("Google") == (1, "month")


def test_period_based_company_only_counts_this_periods_applications(db, monkeypatch):
    """Google is a confirmed month-based cap -- an application from last
    month must not count against this month's allowance."""
    monkeypatch.setattr(config, "load_settings", lambda: {})
    now = datetime.now(timezone.utc)
    last_month = (now.replace(day=1) - timedelta(days=1)).isoformat()
    this_month = now.isoformat()

    _insert(db, "u1", "Google", last_month)
    _insert(db, "u2", "Google", this_month)

    status = company_limits.status_for(db, "Google")
    assert status["applied"] == 1
    assert status["limit"] == 3
    assert status["at_cap"] is False


def test_blanket_default_is_a_lifetime_cap_not_monthly(db, monkeypatch):
    """Unlisted companies get a "total" cap -- it never resets, so an
    application from months ago still counts against it today."""
    monkeypatch.setattr(config, "load_settings", lambda: {})
    now = datetime.now(timezone.utc)
    long_ago = (now - timedelta(days=400)).isoformat()

    for i in range(company_limits.DEFAULT_LIMIT):
        _insert(db, f"u{i}", "Wayne Enterprises", long_ago)

    status = company_limits.status_for(db, "Wayne Enterprises")
    assert status["period"] == "total"
    assert status["applied"] == company_limits.DEFAULT_LIMIT
    assert status["at_cap"] is True


def test_status_for_at_cap(db, monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {})
    now = datetime.now(timezone.utc).isoformat()
    for i in range(3):
        _insert(db, f"u{i}", "Google", now)

    status = company_limits.status_for(db, "Google")
    assert status["applied"] == 3
    assert status["limit"] == 3
    assert status["at_cap"] is True
    assert status["remaining"] == 0


def test_all_statuses_merges_spelling_variants(db, monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {})
    now = datetime.now(timezone.utc).isoformat()
    _insert(db, "u1", "Meta", now)
    _insert(db, "u2", "Meta Platforms, Inc.", now)
    _insert(db, "u3", "Netflix", now)

    rows = {r["company"]: r for r in company_limits.all_statuses(db)}
    assert len(rows) == 2
    meta = next(v for k, v in rows.items() if "Meta" in (k or ""))
    assert meta["applied"] == 2
