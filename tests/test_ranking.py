"""Tests for the pay/prestige/location ranking rework.

Two real defects are pinned here, because both failed silently rather than
erroring and both cost real applications:

1. Pay contributed nothing to a New York posting's desirability. Location and
   pay were one folded component, and the preferred city returned a flat 10.0,
   so every NYC new-grad role at the same prestige scored identically from
   $83k to $354k.
2. Skills fit dominated the ranking at weight 0.7 and buried exactly the
   employers worth applying to -- Meta's internships average a fit of 3.4 and
   Anthropic's new-grad roles a 2.0, both at prestige 10.
"""

import pytest

from applypilot import config
from applypilot.database import init_db
from applypilot.scoring import scorer
from applypilot.web import queries


# ---------------------------------------------------------------------------
# pay is its own axis now
# ---------------------------------------------------------------------------

def test_pay_score_rises_with_pay_for_new_grad():
    """The bug in one assertion: more money must score higher."""
    low = scorer._pay_tier_score("$90,000 - $90,000/yr", is_internship=False)
    mid = scorer._pay_tier_score("$120,000 - $120,000/yr", is_internship=False)
    high = scorer._pay_tier_score("$180,000 - $180,000/yr", is_internship=False)
    assert low < mid < high
    assert high == pytest.approx(10.0)


def test_pay_score_is_continuous_not_banded():
    """The old function returned the same number across a $60k spread, which
    is what made pay invisible once it was averaged in."""
    a = scorer._pay_tier_score("$120,000 - $120,000/yr", is_internship=False)
    b = scorer._pay_tier_score("$135,000 - $135,000/yr", is_internship=False)
    assert a != b


def test_internship_and_new_grad_read_different_curves():
    """$45/hr is an excellent internship and a merely acceptable full-time
    salary, so one shared curve would misjudge one of the two lanes."""
    as_intern = scorer._pay_tier_score("$45-$45/hr", is_internship=True)
    as_full_time = scorer._pay_tier_score("$45-$45/hr", is_internship=False)
    assert as_intern > as_full_time


def test_unstated_pay_is_neutral_not_zero():
    """128 of the 470 eligible high-prestige internships state no salary.
    Scoring those 0 would bury exactly the postings worth seeing."""
    assert scorer._pay_tier_score(None, is_internship=True) == 5.0
    assert scorer._pay_tier_score("N/A", is_internship=False) == 5.0


# ---------------------------------------------------------------------------
# location no longer swallows pay
# ---------------------------------------------------------------------------

def test_location_score_ignores_pay_entirely():
    """_location_desirability takes no salary argument any more -- that
    coupling was the bug."""
    assert scorer._location_desirability("New York, NY", "New York") == 10.0
    assert scorer._location_desirability("Seattle, WA", "New York") == 7.0
    assert scorer._location_desirability("Niles, IL", "New York") == 4.0
    assert scorer._location_desirability("", "New York") == 5.0


# ---------------------------------------------------------------------------
# company tiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Meta Platforms, Inc.", "tier1"),
    ("META", "tier1"),
    ("Amazon Web Services (AWS)", "tier1"),
    ("Google LLC", "tier1"),
    ("Datadog", "adjacent"),
    ("Jane Street Capital", "adjacent"),
    # The reason matching isn't a bare LIKE 'block%': "Block" is on the
    # adjacent list and would otherwise swallow anything starting with it.
    ("Blockchain Widgets", None),
    ("SmallCo Consulting", None),
])
def test_company_tier_matching(tmp_path, name, expected):
    conn = init_db(tmp_path / "tier.db")
    conn.execute(
        "INSERT INTO jobs (url, company, title, scored_at, company_prestige)"
        " VALUES (?,?,?,?,?)",
        ("u1", name, "Engineer", "2026-09-06T00:00:00", 5),
    )
    conn.commit()
    scorer.compute_company_tiers(conn=conn)
    got = conn.execute("SELECT company_tier FROM jobs WHERE url='u1'").fetchone()[0]
    assert got == expected


def test_high_prestige_counts_as_adjacent_even_when_unlisted(tmp_path):
    """The list is a floor, not a ceiling -- a company nobody thought to add
    still surfaces if the scorer rated it a 9 or 10."""
    conn = init_db(tmp_path / "tier2.db")
    conn.execute(
        "INSERT INTO jobs (url, company, title, scored_at, company_prestige)"
        " VALUES (?,?,?,?,?)",
        ("u1", "Some Unlisted Unicorn", "Engineer", "2026-09-06T00:00:00", 9),
    )
    conn.commit()
    scorer.compute_company_tiers(conn=conn)
    got = conn.execute("SELECT company_tier FROM jobs WHERE url='u1'").fetchone()[0]
    assert got == "adjacent"


# ---------------------------------------------------------------------------
# the tier pin, end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def ranked_db(tmp_path):
    conn = init_db(tmp_path / "ranked.db")
    rows = [
        # url, company, tier, fit, desirability, prestige, pay_max_hourly
        ("meta", "Meta", "tier1", 3, 7.0, 10, None),
        ("good", "SmallCo", None, 10, 9.5, 5, 60.0),
        ("mid", "MidCo", None, 8, 8.0, 6, 50.0),
    ]
    for url, co, tier, fit, des, pres, pay in rows:
        conn.execute(
            "INSERT INTO jobs (url, company, title, site, posted_date, fit_score,"
            " desirability_score, company_prestige, company_tier, job_type,"
            # Browse unconditionally requires full_description IS NOT NULL
            # (see queries._filter_clauses) -- without it every list_jobs()
            # call below silently returns no rows.
            " pay_max_hourly, eligible, full_description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, co, f"{co} Engineer", "Intern List - SWE", "2026-09-03T10:00:00",
             fit, des, pres, tier, "new_grad", pay, "yes", f"{co} is hiring."),
        )
    conn.commit()
    return conn


def test_top_sort_pins_big_tech_above_a_better_scoring_row(ranked_db):
    """Meta scores worse on every visible number and must still come first."""
    res = queries.list_jobs({}, sort="top", conn=ranked_db)
    assert [r["url"] for r in res["rows"]][0] == "meta"


def test_include_tier_exempts_big_tech_from_every_bar(ranked_db):
    """A prestige-10 posting with a fit of 3 and no stated pay is precisely
    what the priority panels used to drop."""
    strict = queries.list_jobs(
        {"min_fit": 8, "min_prestige": 9, "min_pay": 40}, sort="top", conn=ranked_db)
    assert "meta" not in [r["url"] for r in strict["rows"]]

    exempt = queries.list_jobs(
        {"min_fit": 8, "min_prestige": 9, "min_pay": 40, "include_tier": True},
        sort="top", conn=ranked_db)
    assert "meta" in [r["url"] for r in exempt["rows"]]


def test_tier_only_narrows_to_big_tech(ranked_db):
    res = queries.list_jobs({"tier_only": True}, sort="top", conn=ranked_db)
    assert [r["url"] for r in res["rows"]] == ["meta"]


def test_default_sort_is_newest_posted():
    """DEFAULT_SORT moved from the tier-pinned "top" preset to "posted" once
    sortable column headers shipped -- "top" is a preset a chip applies, not
    a clickable column, and DayTable now opens on posting date, newest
    first, by default (see DayTable.tsx and queries._SORT_COLUMNS)."""
    assert queries.DEFAULT_SORT == "posted"


# ---------------------------------------------------------------------------
# one resume, one graduation date
# ---------------------------------------------------------------------------

def test_there_is_exactly_one_resume_and_one_grad_date():
    """The second 'returning student' identity was scrapped. Nothing should
    be able to reintroduce a variant axis by config alone."""
    assert not hasattr(config, "get_resume_variant_paths")
    assert "resume_variants" not in config.DEFAULT_SETTINGS
    assert config.DEFAULT_SETTINGS["graduation_date"] == "May 2027"
    txt, pdf = config.get_resume_paths()
    assert txt == config.RESUME_PATH and pdf == config.RESUME_PDF_PATH


# ---------------------------------------------------------------------------
# the sibling-company sweep
# ---------------------------------------------------------------------------

@pytest.fixture
def sibling_db(tmp_path, monkeypatch):
    from applypilot import database
    conn = init_db(tmp_path / "sib.db")
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)
    from applypilot.apply import launcher
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: conn)
    return conn


def _add(conn, url, company, tier=None):
    conn.execute(
        "INSERT INTO jobs (url, company, title, job_type, eligible,"
        " is_terminal_internship, company_tier) VALUES (?,?,?,?,?,?,?)",
        (url, company, "Intern", "internship", "yes", "yes", tier),
    )


def test_small_employer_siblings_are_disqualified(sibling_db):
    """The original behaviour, and still right for it: five near-identical
    postings at one small company really do share one form."""
    from applypilot.apply.launcher import _clear_terminal_flags_on_grad_date_mismatch

    for i in range(4):
        _add(sibling_db, f"verkada{i}", "Verkada")
    sibling_db.commit()

    _clear_terminal_flags_on_grad_date_mismatch("verkada0")
    rows = dict(sibling_db.execute(
        "SELECT url, eligible FROM jobs WHERE company='Verkada'").fetchall())
    assert rows["verkada1"] == "no"


def test_big_tech_siblings_are_flagged_not_killed(sibling_db):
    """Amazon runs 29 distinct internships here. One form mismatch is not
    evidence about the rest, and killing them contradicts the standing
    requirement that every big-tech posting gets applied to."""
    from applypilot.apply.launcher import _clear_terminal_flags_on_grad_date_mismatch

    for i in range(4):
        _add(sibling_db, f"amzn{i}", "Amazon", tier="tier1")
    sibling_db.commit()

    _clear_terminal_flags_on_grad_date_mismatch("amzn0")
    rows = dict(sibling_db.execute(
        "SELECT url, eligible FROM jobs WHERE company='Amazon'").fetchall())
    assert rows["amzn1"] == "unclear", "sibling must stay applyable"
    assert rows["amzn2"] == "unclear"
    # The originating job's own terminal flag is still cleared -- that one
    # really did hit the wall.
    flag = sibling_db.execute(
        "SELECT is_terminal_internship FROM jobs WHERE url='amzn0'").fetchone()[0]
    assert flag == "no"


def test_large_employer_siblings_are_flagged_even_without_a_tier(sibling_db):
    """The count guard stands alone: an unlisted company posting a dozen
    internships is running more than one program too."""
    from applypilot.apply.launcher import _clear_terminal_flags_on_grad_date_mismatch

    for i in range(12):
        _add(sibling_db, f"big{i}", "BigUnlistedCo")
    sibling_db.commit()

    _clear_terminal_flags_on_grad_date_mismatch("big0")
    row = sibling_db.execute(
        "SELECT eligible FROM jobs WHERE url='big1'").fetchone()[0]
    assert row == "unclear"
