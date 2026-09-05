"""Housing / relocation detection.

Strings are taken from real postings. The function's whole job is deciding
whether an offer exists -- no dollar amounts are parsed, because measuring
1090 internships showed only 3 state a figure and the pay tier discarded the
precision anyway. That also removes every wage-mistaken-for-stipend bug.
"""

import pytest

from applypilot.scoring.scorer import offers_housing


@pytest.mark.parametrize("text", [
    "A $2,000 housing stipend for students relocating more than 50 miles",
    "Interns who relocate to Illinois will receive a housing stipend to cover living expenses",
    "Housing Stipend Available",
    "Interns will receive relocation benefits and short-term housing",
    "Full housing and relocation for co-ops outside the DC metro area",
    "Relocation assistance available",
    "Interns will receive a monthly housing stipend",
])
def test_offers_are_detected(text):
    assert offers_housing(text) is True


@pytest.mark.parametrize("text", [
    "No Corporate Housing Provided",
    "2026 Paid Internship No Corporate housing is offered and/or available",
    "Housing is not provided for this role",
])
def test_explicit_refusal_is_not_an_offer(text):
    assert offers_housing(text) is False


@pytest.mark.parametrize("text", [
    # "warehousing" contains "housing" -- PepsiCo matched on it before word
    # boundaries were added.
    "They manage large scale data warehousing services and solutions",
    # The bare noun is not an offer.
    "Freddie Mac is a housing finance company whose Multifamily team builds apps",
    "Compensation is $45/hr.",
    "",
])
def test_mentions_that_are_not_offers(text):
    assert offers_housing(text) is False


def test_a_wage_beside_the_housing_word_is_still_just_an_offer():
    """Ramp: "The monthly rate for this internship is $11,700 USD + housing
    stipend". Parsing figures read the 11,700 -- the monthly wage -- as a
    stipend worth $24.38/hour, the largest in the corpus and entirely wrong.
    Detection-only cannot make that mistake: the posting offers housing, full
    stop."""
    assert offers_housing(
        "The monthly rate for this internship is $11,700 USD + housing stipend") is True


def test_missing_description_is_safe():
    assert offers_housing(None) is False
