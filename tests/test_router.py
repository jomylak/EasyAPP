"""Routing regression tests.

The cases here are not hypothetical -- every one is a real posting from the
job table that the router got wrong at some point during development, or a
boundary that a plausible-looking edit to the term lists would break.
"""

import pytest

from applypilot.scoring.router import (
    AIML,
    DATA,
    NON_DISCRIMINATIVE,
    SWE,
    _TRACK_TERMS,
    _WEAK_TERMS,
    explain_route,
    route_resume_track,
)


@pytest.mark.parametrize("title,expected", [
    # Titles naming a track outright.
    ("Machine Learning Engineer II (Intern)", AIML),
    ("Artificial Intelligence (AI) Engineering Intern", AIML),
    ("Gen AI Intern", AIML),
    ("Data Engineer Intern", DATA),
    ("Data Science Intern", DATA),
    ("Frontend Engineer Intern", SWE),
    ("Full Stack Developer Intern", SWE),
])
def test_unambiguous_titles(title, expected):
    assert route_resume_track({"title": title}) == expected


@pytest.mark.parametrize("title", [
    # Every one of these matches BOTH the SWE and AI/ML vocabularies. They are
    # AI roles that merely happen to say "Software Engineer", so precedence
    # must put AI/ML first -- a SWE-first order sends all of them to the
    # generic resume, which is the single worst routing failure available.
    "2027 Software Engineering Intern - Agentic AI & Workflow Automation",
    "AI Foundations - Software Engineer - Research Internship 2027",
    "Software Engineer, Foundation AI",
    "Software Engineer, Applied AI (Starlink)",
    "Software Engineer, AI/ML Infrastructure (US-Based)",
    "SAP iXp Intern - Full-Stack AI Developer",
    "Software Engineering Intern - Salesforce & Agentic AI",
])
def test_aiml_beats_swe_on_collision(title):
    assert route_resume_track({"title": title}) == AIML


def test_bare_ai_decides_a_title_but_not_a_keyword_list():
    """The weak tier exists because of this asymmetry.

    Bare "AI" fired 38 times across the tailorable set -- more than every
    genuine AI term combined -- because postings list "AI tools" as
    boilerplate in long keyword lists. Trusting it there sent "IoT Engineer"
    and "Technical Intern 3" to the AI/ML resume.
    """
    assert route_resume_track({"title": "AI Intern"}) == AIML
    assert route_resume_track({
        "title": "IoT Engineer",
        "keywords": "Python, Java, AI tools, REST APIs, MySQL",
    }) == SWE


def test_strong_keywords_still_rescue_an_uninformative_title():
    """Real posting: the title says nothing, the keywords say PyTorch."""
    assert route_resume_track({
        "title": "Technical Intern 3",
        "keywords": "Java, Python, Machine Learning, PyTorch, Data Structures",
    }) == AIML


def test_cascade_stops_at_the_first_field_that_matches():
    """A description that mentions AI once must not outvote the title.

    Pooling all three fields into one bag of terms would let boilerplate in an
    "about us" paragraph decide the resume.
    """
    job = {
        "title": "Frontend Engineer Intern",
        "keywords": "React, TypeScript, CSS",
        "full_description": "We are an AI-first company using machine learning.",
    }
    assert route_resume_track(job) == SWE
    assert explain_route(job)["matched_on"] == "title"


def test_falls_back_to_swe_when_nothing_matches():
    job = {"title": "Technical Intern 4"}
    assert route_resume_track(job) == SWE
    assert explain_route(job)["matched_on"] == "fallback"


def test_missing_and_empty_fields_are_safe():
    assert route_resume_track({}) == SWE
    assert route_resume_track({"title": None, "keywords": None}) == SWE
    assert route_resume_track({"title": ""}) == SWE


def test_no_non_discriminative_term_leaks_into_a_track_list():
    """Terms common to all three tracks add a constant hit to every bucket.

    They cancel out at best and mislead at worst, so they are excluded rather
    than listed everywhere. This guards against someone "helpfully" adding
    Python or AWS to a list later.
    """
    for track, terms in _TRACK_TERMS.items():
        for term in terms:
            bare = term.replace(r"\b", "").replace("\\", "").lower()
            assert bare not in NON_DISCRIMINATIVE, f"{term!r} in {track} list"


def test_weak_terms_are_not_also_strong_terms():
    """A term in both tiers would silently defeat the title-only restriction."""
    for track, weak in _WEAK_TERMS.items():
        assert not (set(weak) & set(_TRACK_TERMS[track])), track


def test_word_boundaries_prevent_substring_matches():
    """'spark' must not fire on 'sparkling', 'ios' must not fire on 'curious'."""
    assert route_resume_track({"title": "Curious Engineer, Sparkling Water Co"}) == SWE
