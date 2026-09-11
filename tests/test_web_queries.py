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
            # Browse unconditionally requires full_description IS NOT NULL
            # (see queries._filter_clauses) -- an unenriched row was never a
            # real posting to begin with, but every fixture row here already
            # stands in for an enriched one, so it needs a value or every
            # query in this file silently returns nothing.
            " keywords, full_description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, co, f"{co} Engineer", "Intern List - SWE", posted, disc,
             fit, des, pres, jt, ats, elig, "Python, Java", f"{co} is hiring an engineer."),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# day bucketing
# ---------------------------------------------------------------------------

def test_days_are_newest_first_with_counts(db):
    days = queries.list_days(db)
    assert [d["day"] for d in days] == ["2026-09-03", "2026-09-02"]
    # u4 is eligible='no' -- must not count toward a day total the job list
    # itself won't show, same eligibility gate list_jobs() enforces.
    assert days[0]["total"] == 3


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
    # u4 is eligible='no' -- eligibility is enforced unconditionally now, see
    # test_eligibility_is_always_enforced below.
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert res["total"] == 3
    assert {r["url"] for r in res["rows"]} == {"u1", "u2", "u3"}


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
    assert res["total"] == 3
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6


def test_pagination_covers_every_row_exactly_once(db):
    seen = []
    for page in range(4):
        res = queries.list_jobs({"day": "2026-09-03"}, page=page, page_size=2, conn=db)
        seen += [r["url"] for r in res["rows"]]
    assert sorted(seen) == ["u1", "u2", "u3"]


def test_eligibility_is_always_enforced(db):
    """Not an opt-in flag any more -- there is no reason this table should
    ever surface a job that can't honestly be applied to. 'unclear'/NULL
    still passes: a wrong reject costs a real opportunity, and plenty of
    rows were scored before the gate existed."""
    res = queries.list_jobs({"day": "2026-09-02"}, conn=db)
    assert {r["url"] for r in res["rows"]} == {"u5", "u6"}   # both NULL, both kept

    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert "u4" not in {r["url"] for r in res["rows"]}   # explicit 'no', dropped


def test_decided_jobs_are_always_hidden(db):
    """Not an opt-in flag any more -- once a job is queued, in flight, or
    applied, Browse must stop showing it everywhere without the user having
    to remember to toggle a pill. A 'failed' row stays visible (it's the
    Retry case)."""
    db.execute("UPDATE jobs SET apply_status = 'queued' WHERE url = 'u1'")
    db.execute("UPDATE jobs SET apply_status = 'in_progress' WHERE url = 'u2'")
    db.execute("UPDATE jobs SET apply_status = 'applied' WHERE url = 'u3'")
    db.commit()
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert {r["url"] for r in res["rows"]} == set()

    db.execute("UPDATE jobs SET apply_status = 'failed' WHERE url = 'u1'")
    db.commit()
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert {r["url"] for r in res["rows"]} == {"u1"}


def test_confirmed_duplicates_are_always_hidden(db):
    """Not an opt-in flag either -- fit_gate_sql() already excludes
    duplicate_of rows from the ranked apply queue and from
    scoring/tailoring, so Browse must match or a duplicate posting can be
    selected and queued/applied to separately from its canonical row."""
    db.execute("UPDATE jobs SET duplicate_of = 'u2' WHERE url = 'u1'")
    db.commit()
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    assert "u1" not in {r["url"] for r in res["rows"]}


@pytest.mark.parametrize("f,expected", [
    ({"min_fit": 9}, {"u1", "u2", "u3"}),   # u3 is fit 10
    ({"min_fit": 10}, {"u1", "u3"}),
    ({"min_prestige": 10}, {"u1", "u2"}),
    ({"min_desirability": 8.6}, {"u1"}),
    ({"job_type": "new_grad"}, {"u2"}),
    ({"ats": ["Ashby"]}, {"u3"}),            # u4 also matches Ashby but is eligible='no'
    ({"ats": ["Ashby", "Greenhouse"]}, {"u2", "u3"}),
    ({"q": "SpaceX"}, {"u2"}),
])
def test_filters(db, f, expected):
    res = queries.list_jobs({"day": "2026-09-03", **f}, conn=db)
    assert {r["url"] for r in res["rows"]} == expected


def test_internship_term_filter_keeps_spring_and_summer_drops_fall_winter(db):
    """Not an opt-in flag any more -- Spring and Summer are the candidate's
    only two workable terms, so this always applies to internships. Spring
    used to require the title ALSO say Summer to survive; that was a bug,
    not a feature -- a Spring-only posting is explicitly wanted now."""
    db.execute("UPDATE jobs SET title = 'Winter 2027 Software Engineering Intern' WHERE url = 'u1'")
    db.execute("UPDATE jobs SET title = 'Fall 2027 Data Science Intern' WHERE url = 'u3'")
    db.execute(
        "UPDATE jobs SET title = 'Spring 2027 Software Engineering Intern', "
        "eligible = 'yes' WHERE url = 'u4'"
    )
    db.commit()
    res = queries.list_jobs({"day": "2026-09-03"}, conn=db)
    urls = {r["url"] for r in res["rows"]}
    assert "u1" not in urls and "u3" not in urls
    assert {"u2", "u4"} <= urls   # u2 has no season keyword; u4 is Spring-only

    db.execute("UPDATE jobs SET title = 'Summer 2027 Software Engineering Intern' WHERE url = 'u6'")
    db.commit()
    res = queries.list_jobs({"day": "2026-09-02"}, conn=db)
    assert "u6" in {r["url"] for r in res["rows"]}


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
        " desirability_score, company_prestige, full_description) VALUES (?,?,?,?,?,?,?,?,?)",
        ("recent1", "Recent Co", "Engineer", "Intern List - SWE", recent, 8, 8.0, 8,
         "Recent Co is hiring an engineer."),
    )
    db.execute(
        "INSERT INTO jobs (url, company, title, site, posted_date, fit_score,"
        " desirability_score, company_prestige, full_description) VALUES (?,?,?,?,?,?,?,?,?)",
        ("stale1", "Stale Co", "Engineer", "Intern List - SWE", stale, 8, 8.0, 8,
         "Stale Co is hiring an engineer."),
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
    # spend/priced_attempts are scoped to apply_backend='goose' (see
    # queries.stats' docstring -- Claude-backend rows cost ~30x more and
    # would skew the average), so a fixture row exercising them needs the
    # backend set, not just the cost.
    db.execute("UPDATE jobs SET apply_status='applied', apply_cost_usd=1.5, "
               "apply_backend='goose' WHERE url='u1'")
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
    db.execute("UPDATE jobs SET apply_status='failed', apply_cost_usd=0.9, "
               "apply_backend='goose' WHERE url='u1'")
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
