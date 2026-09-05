"""Tests for the web UI's read layer.

The browse tab is a lot of SQL built from user-supplied filters, and its
failure mode is silent: a wrong clause returns a plausible-looking shorter
list rather than an error. These cases pin the behaviour that a person
triaging four hundred jobs a day is relying on without being able to see it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.database import init_db, _DAY_EXPR
from applypilot.web import queries


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "web.db")
    rows = [
        # url, company, posted, discovered, fit, des, prestige, type, ats, elig
        ("u1", "Microsoft", "2026-09-03T10:00:00", None, 10, 8.8, 10, "internship", "Workday", "yes"),
        ("u2", "SpaceX",    "2026-09-03T09:00:00", None,  9, 8.5, 10, "new_grad",   "Greenhouse", "yes"),
        ("u3", "Hadrian",   "2026-09-03T08:00:00", None, 10, 8.5,  8, "internship", "Ashby", "yes"),
        ("u4", "SmallCo",   "2026-09-03T07:00:00", None,  4, 3.0,  3, "internship", "Ashby", "no"),
        ("u5", "OtherDay",  "2026-09-02T10:00:00", None,  9, 9.0,  9, "new_grad",   "Workday", None),
        # no posted_date: must fall back to discovered_at, not vanish
        ("u6", "NoPosted",  None, "2026-09-02T11:00:00", 8, 7.0,  7, "internship", None, None),
    ]
    for url, co, posted, disc, fit, des, pres, jt, ats, elig in rows:
        conn.execute(
            "INSERT INTO jobs (url, company, title, site, posted_date, discovered_at,"
            " fit_score, desirability_score, company_prestige, job_type, ats, eligible,"
            " keywords) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, co, f"{co} Engineer", "Intern List - SWE", posted, disc,
             fit, des, pres, jt, ats, elig, "Python, Java"),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# day bucketing
# ---------------------------------------------------------------------------

def test_days_are_newest_first_with_counts(db):
    days = queries.list_days(db)
    assert [d["day"] for d in days] == ["2026-09-03", "2026-09-02"]
    assert days[0]["total"] == 4


def test_a_job_with_no_posted_date_still_lands_on_a_day(db):
    """Discovery leaves posted_date NULL for some sources. Those jobs must
    bucket by discovered_at rather than disappearing from the browse tab."""
    days = {d["day"]: d for d in queries.list_days(db)}
    assert days["2026-09-02"]["total"] == 2   # u5 (posted) + u6 (discovered)


def test_chip_counts_match_the_filters_they_label(db):
    day = next(d for d in queries.list_days(db) if d["day"] == "2026-09-03")
    # Best Fit is desirability >= 6 AND fit >= 8: u1, u2, u3 -- not u4.
    assert day["best_fit"] == 3
    # Top Prestige is >= 9: u1, u2 -- not u3 at 8.
    assert day["prestige"] == 2


# ---------------------------------------------------------------------------
# the day table itself
# ---------------------------------------------------------------------------

def test_a_day_query_only_returns_that_day(db):
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert res["total"] == 4
    assert {r["url"] for r in res["rows"]} == {"u1", "u2", "u3", "u4"}


@pytest.mark.parametrize("sort,first", [
    ("prestige", "u1"),      # prestige 10, then fit 10 beats SpaceX's 9
    ("fit", "u1"),           # fit 10, then desirability 8.8 beats Hadrian's 8.5
    ("desirability", "u1"),
    ("company", "u3"),       # Hadrian < Microsoft < SmallCo < SpaceX
    ("posted", "u1"),        # most recent posting that day
])
def test_each_day_sorts_independently(db, sort, first):
    res = queries.list_jobs({"day": "2026-09-03"}, sort=sort, conn=db)
    assert res["rows"][0]["url"] == first


def test_an_unknown_sort_falls_back_instead_of_erroring(db):
    """The sort key arrives from a query string; it must never reach SQL raw."""
    res = queries.list_jobs({"day": "2026-09-03"}, sort="; DROP TABLE jobs--", conn=db)
    assert res["sort"] == queries.DEFAULT_SORT
    assert res["total"] == 4
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6


def test_pagination_covers_every_row_exactly_once(db):
    seen = []
    for page in range(4):
        res = queries.list_jobs({"day": "2026-09-03"}, page=page, page_size=2, conn=db)
        seen += [r["url"] for r in res["rows"]]
    assert sorted(seen) == ["u1", "u2", "u3", "u4"]


def test_eligible_filter_keeps_unknowns(db):
    """'unclear'/NULL passes: a wrong reject costs a real opportunity, and
    plenty of rows were scored before the gate existed."""
    res = queries.list_jobs({"day": "2026-09-02", "eligible_only": True}, conn=db)
    assert {r["url"] for r in res["rows"]} == {"u5", "u6"}   # both NULL, both kept


def test_eligible_filter_drops_explicit_no(db):
    res = queries.list_jobs({"day": "2026-09-03", "eligible_only": True}, conn=db)
    assert "u4" not in {r["url"] for r in res["rows"]}


@pytest.mark.parametrize("f,expected", [
    ({"min_fit": 9}, {"u1", "u2", "u3"}),   # u3 is fit 10
    ({"min_fit": 10}, {"u1", "u3"}),
    ({"min_prestige": 10}, {"u1", "u2"}),
    ({"min_desirability": 8.6}, {"u1"}),
    ({"job_type": "new_grad"}, {"u2"}),
    ({"ats": "Ashby"}, {"u3", "u4"}),
    ({"q": "SpaceX"}, {"u2"}),
])
def test_filters(db, f, expected):
    res = queries.list_jobs({"day": "2026-09-03", **f}, conn=db)
    assert {r["url"] for r in res["rows"]} == expected


def test_posted_within_days_uses_real_now_not_fixture_dates(db):
    """The fixture rows are pinned to fixed dates, but `posted_within_days` is
    relative to the actual clock -- so this inserts its own rows relative to
    `datetime.now()` rather than reusing the fixture's 2026-09-0x dates, which
    would silently stop meaning "recent" the day this test outlives them."""
    today = datetime.now(timezone.utc)
    recent = (today - timedelta(days=1)).isoformat()
    stale = (today - timedelta(days=30)).isoformat()
    db.execute(
        "INSERT INTO jobs (url, company, title, site, posted_date, fit_score,"
        " desirability_score, company_prestige) VALUES (?,?,?,?,?,?,?,?)",
        ("recent1", "Recent Co", "Engineer", "Intern List - SWE", recent, 8, 8.0, 8),
    )
    db.execute(
        "INSERT INTO jobs (url, company, title, site, posted_date, fit_score,"
        " desirability_score, company_prestige) VALUES (?,?,?,?,?,?,?,?)",
        ("stale1", "Stale Co", "Engineer", "Intern List - SWE", stale, 8, 8.0, 8),
    )
    db.commit()
    res = queries.list_jobs({"posted_within_days": 7}, conn=db)
    urls = {r["url"] for r in res["rows"]}
    assert "recent1" in urls
    assert "stale1" not in urls


def test_browse_rows_do_not_carry_full_descriptions(db):
    """view.py inlined every description and produced a 21 MB page. A day of
    400 rows must not repeat that."""
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert "full_description" not in res["rows"][0]


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------

def test_detail_strips_the_keywords_duplicated_into_score_reasoning(db):
    """scorer.py writes score_reasoning as "keywords\\nreasoning" and keywords
    is also its own column. Showing both would print it twice."""
    db.execute("UPDATE jobs SET keywords = ?, score_reasoning = ? WHERE url = 'u1'",
               ("Python, Java", "Python, Java\nStrong systems match."))
    db.commit()
    job = queries.job_detail("u1", conn=db)
    assert job["reasoning"] == "Strong systems match."
    assert job["keywords"] == "Python, Java"


def test_detail_of_a_missing_job_is_none(db):
    assert queries.job_detail("nope", conn=db) is None


# ---------------------------------------------------------------------------
# stats + facets
# ---------------------------------------------------------------------------

def test_stats_counts_apply_states(db):
    db.execute("UPDATE jobs SET apply_status='applied', apply_cost_usd=1.5 WHERE url='u1'")
    db.execute("UPDATE jobs SET apply_status='queued' WHERE url='u2'")
    db.commit()
    s = queries.stats(db)
    assert (s["applied"], s["queued"], s["total"]) == (1, 1, 6)
    assert s["spend"] == pytest.approx(1.5)
    assert s["priced_attempts"] == 1


def test_retrying_a_failed_job_does_not_change_priced_attempts(db):
    # A failed attempt that already spent money, then retried (Dashboard's
    # Retry button flips apply_status back to 'queued' without touching the
    # historical apply_cost_usd). priced_attempts must stay the same so
    # avg-cost-per-attempt doesn't jump from a retry click alone.
    db.execute("UPDATE jobs SET apply_status='failed', apply_cost_usd=0.9 WHERE url='u1'")
    db.commit()
    before = queries.stats(db)
    assert before["priced_attempts"] == 1
    assert before["spend"] == pytest.approx(0.9)

    db.execute("UPDATE jobs SET apply_status='queued' WHERE url='u1'")
    db.commit()
    after = queries.stats(db)
    assert after["priced_attempts"] == before["priced_attempts"]
    assert after["spend"] == pytest.approx(before["spend"])


def test_facets_skip_nulls(db):
    f = queries.facets(db)
    assert None not in f["ats"] and "" not in f["ats"]
    assert f["job_types"] == ["internship", "new_grad"]


def test_day_expression_matches_the_indexed_one(db):
    """The expression index is only used when the query spells the expression
    exactly as the index does. A cosmetic edit to either silently turns every
    day query into a full table scan."""
    plan = db.execute(
        f"EXPLAIN QUERY PLAN SELECT url FROM jobs WHERE {_DAY_EXPR} = '2026-09-03'"
    ).fetchall()
    assert any("idx_jobs_day" in str(tuple(r)) for r in plan), plan
    assert _DAY_EXPR in queries._ROW_COLUMNS
