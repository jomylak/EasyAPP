"""Tests for the batch cost estimator.

The estimator's job is not precision -- there is not enough history for that.
It is honesty: never price a batch at zero, never present a guess with the
same confidence as a measurement, and never quietly assume every application
succeeds.
"""

import itertools

import pytest

from applypilot import costs
from applypilot.database import init_db


_seq = itertools.count()


def _seed(conn, rows):
    """Insert historical runs. URLs just have to be unique, not meaningful."""
    for backend, ats, cost in rows:
        conn.execute(
            "INSERT INTO jobs (url, apply_backend, ats, apply_cost_usd) "
            "VALUES (?, ?, ?, ?)",
            (f"https://history.example/{next(_seq)}", backend, ats, cost),
        )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "costs.db")


def test_empty_selection_costs_nothing(db):
    est = costs.estimate_batch([], "goose", conn=db)
    assert est["expected"] == 0.0
    assert est["n_jobs"] == 0


def test_no_history_falls_back_to_configured_default(db):
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', 'Workday')")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["n_samples"] == 0
    assert est["basis"] == "configured default"
    # The headline number must never be zero -- a free batch is a lie that
    # would read as "this costs nothing, tick everything".
    assert est["expected"] > 0


def test_uses_backend_median_once_there_is_history(db):
    _seed(db, [("goose", None, 0.10), ("goose", None, 0.20), ("goose", None, 0.30)])
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', NULL)")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["expected"] == pytest.approx(0.20)   # median, not mean
    assert est["n_samples"] == 3
    assert est["basis"] == "goose, all sites"


def test_median_resists_one_outlier(db):
    """A single runaway run must not set the price for every future batch."""
    _seed(db, [("goose", None, 0.10), ("goose", None, 0.12),
               ("goose", None, 0.11), ("goose", None, 9.99)])
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', NULL)")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["expected"] < 0.5


def test_prefers_the_ats_specific_median_when_well_supported(db):
    # Backend-wide history is cheap; this particular ATS is expensive.
    _seed(db, [("goose", None, 0.05)] * 4)
    _seed(db, [("goose", "Workday", 3.00), ("goose", "Workday", 3.10),
               ("goose", "Workday", 3.20)])
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', 'Workday')")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["expected"] == pytest.approx(3.10)
    assert est["basis"] == "goose on Workday"


def test_thin_ats_history_defers_to_the_backend_median(db):
    """Two observations of one ATS is not enough to override everything else."""
    _seed(db, [("goose", None, 0.05)] * 5)
    _seed(db, [("goose", "Taleo", 9.00), ("goose", "Taleo", 9.50)])
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', 'Taleo')")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["expected"] < 1.0
    assert est["basis"] == "goose, all sites"


def test_failed_runs_count_toward_the_estimate(db):
    """A failed application still spent money; pricing only successes would
    make every batch look cheaper than it is."""
    db.execute("INSERT INTO jobs (url, apply_backend, apply_status, apply_cost_usd, ats)"
               " VALUES ('https://f/1', 'goose', 'failed', 2.00, NULL)")
    db.execute("INSERT INTO jobs (url, apply_backend, apply_status, apply_cost_usd, ats)"
               " VALUES ('https://f/2', 'goose', 'applied', 2.00, NULL)")
    db.execute("INSERT INTO jobs (url, apply_backend, apply_status, apply_cost_usd, ats)"
               " VALUES ('https://f/3', 'goose', 'failed', 2.00, NULL)")
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', NULL)")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["expected"] == pytest.approx(2.00)
    assert est["n_samples"] == 3


def test_batch_scales_with_job_count(db):
    _seed(db, [("goose", None, 0.50)] * 3)
    for i in range(4):
        db.execute("INSERT INTO jobs (url, ats) VALUES (?, NULL)", (f"https://a/{i}",))
    db.commit()

    est = costs.estimate_batch([f"https://a/{i}" for i in range(4)], "goose", conn=db)
    assert est["expected"] == pytest.approx(2.00)
    assert est["n_jobs"] == 4
    assert est["low"] < est["expected"] < est["high"]


def test_reports_the_weakest_evidence_in_a_mixed_batch(db):
    """Two ATSes with different amounts of history: the thinner one sets the
    reported confidence, because the batch is only as well-estimated as its
    least-known member."""
    _seed(db, [("goose", "Workday", 1.0)] * 10)
    _seed(db, [("goose", "Taleo", 2.0)] * 4)
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', 'Workday')")
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/2', 'Taleo')")
    db.commit()

    est = costs.estimate_batch(["https://a/1", "https://a/2"], "goose", conn=db)
    assert est["expected"] == pytest.approx(3.00)   # 1.00 + 2.00, priced per ATS
    assert est["n_samples"] == 4                     # the Taleo count, not the Workday one
    assert "mixed" in est["basis"]


def test_a_job_with_no_ats_history_still_draws_on_the_backend(db):
    """Falling back to the backend-wide median is still evidence, and must not
    be reported as if the estimate came from nowhere."""
    _seed(db, [("goose", "Workday", 1.0)] * 3)
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', 'Greenhouse')")
    db.commit()

    est = costs.estimate_batch(["https://a/1"], "goose", conn=db)
    assert est["n_samples"] == 3
    assert est["basis"] == "goose, all sites"


def test_unknown_backend_is_not_priced_at_zero(db):
    db.execute("INSERT INTO jobs (url, ats) VALUES ('https://a/1', NULL)")
    db.commit()
    est = costs.estimate_batch(["https://a/1"], "some-new-backend", conn=db)
    assert est["expected"] > 0
