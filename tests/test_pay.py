"""Tests for normalising stated pay to an hourly figure.

Every case here is a real string from the database. The reason this exists is
that `salary` is display text, and filtering or sorting on it directly
compares "$9" against "$110500" lexically and puts the nine first.
"""

import pytest

from applypilot.pay import to_hourly


@pytest.mark.parametrize("salary,lo,hi", [
    # the four period formats, all normalised to $/hr
    ("$35-$50/hr",              35.0,  50.0),
    ("$110500-$160000/yr",      53.12, 76.92),
    ("$6k-$11k/mon",            34.62, 63.46),
    ("$5k-$6k/wk",             125.0, 150.0),
    # decimals and thousands separators survive
    ("$20.27-$22.30/hr",        20.27, 22.30),
    ("$82039.23-$112803.95/yr", 39.44, 54.23),
    ("$1,050-$1,200/wk",        26.25, 30.0),
    # a single figure, not a range
    ("$45/hr",                  45.0,  45.0),
    # a "range" with both ends equal is common and not an error
    ("$32.50-$32.50/hr",        32.5,  32.5),
])
def test_parses_real_postings(salary, lo, hi):
    # $110500/yr lands exactly on a half-cent, where round() uses banker's
    # rounding and gives 53.12 rather than 53.13.
    got_lo, got_hi = to_hourly(salary)
    assert got_lo == pytest.approx(lo, abs=0.01)
    assert got_hi == pytest.approx(hi, abs=0.01)


def test_annual_and_monthly_of_the_same_wage_agree():
    """A year is 2080 hours and a month is a twelfth of that, so the same
    salary stated either way must normalise to the same number."""
    yearly, _ = to_hourly("$124800-$124800/yr")
    monthly, _ = to_hourly("$10400-$10400/mon")
    assert yearly == pytest.approx(monthly, rel=1e-3)


@pytest.mark.parametrize("salary", [
    None, "", "N/A",
    "€31270-€40150/yr",   # euros -- converting would invent a rate
    "CAD86600-CAD118600/yr",
    "US18.98-US18.98/hr",
])
def test_unusable_strings_yield_nothing(salary):
    assert to_hourly(salary) == (None, None)


def test_unpaid_is_zero_not_unknown():
    """An unpaid posting has a known wage of zero. Returning None would make
    it 'unknown' and let it slip through a pay floor."""
    assert to_hourly("Unpaid") == (0.0, 0.0)


@pytest.mark.parametrize("salary", [
    "$7650000000-$12134000000/mon",   # real row: a scraping artifact
    "$0-$10000000/yr",                # real row: a placeholder range
    "$0-$10000k/yr",
])
def test_implausible_figures_are_rejected_not_clamped(salary):
    """Discovery sometimes scrapes a number out of the wrong element. One such
    row is enough to wreck a sort or empty out a pay threshold. Rejecting is
    safer than clamping: an invented-but-plausible number cannot be spotted
    downstream."""
    assert to_hourly(salary) == (None, None)


def test_a_zero_upper_bound_means_absent_not_a_range_down_to_zero():
    """Real row: "$129000-$0/yr" is a missing upper bound, and reading it as a
    range would put the job's high end below its low end."""
    lo, hi = to_hourly("$129000-$0/yr")
    assert lo == hi == pytest.approx(62.02, abs=0.01)


def test_a_reversed_range_is_ordered():
    lo, hi = to_hourly("$50-$35/hr")
    assert (lo, hi) == (35.0, 50.0)


def test_a_low_end_below_a_floor_still_reports_its_real_high_end():
    """The $18-$27/hr posting that failed a $30 floor is a real case; both
    ends must survive so a filter can decide which one it cares about."""
    assert to_hourly("$18-$27/hr") == (18.0, 27.0)
