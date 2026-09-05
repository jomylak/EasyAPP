"""Terminal-internship detection.

Every string here is taken from a real posting in the job table. The flag
grants an absolute top-of-queue override, so the asymmetry matters: a false
positive promotes a job the candidate may not even be eligible for, while a
false negative merely leaves a good job ranked on its own merits. These tests
are therefore weighted toward precision.
"""

import pytest

from applypilot.scoring.scorer import terminal_evidence


@pytest.mark.parametrize("text", [
    # Real evidence: the posting names candidates who have finished school.
    "Currently pursuing an undergraduate or graduate degree in computer science, or recently graduated",
    "Currently enrolled in or recently completed an Associate's or Bachelor's degree",
    "Currently pursuing or have recently completed a degree in Computer Science, Web Development",
    "Must be currently enrolled or recently graduated (start date must be within 6 months of graduation date)",
    "We welcome graduating seniors to apply.",
    # UPPER bounds. An earlier graduate satisfies one by definition, and a
    # role you must graduate *before* is one you don't return to school after.
    "Must graduate before December 2027.",
    "The program is open to students graduating from undergraduate or Master's programs by June 2027",
])
def test_explicit_acceptance_is_terminal(text):
    assert terminal_evidence(text) == "yes"


@pytest.mark.parametrize("text", [
    # Silence. The original flag treated all of these as terminal, which was
    # 77.5% of everything it flagged.
    "Currently pursuing a Bachelor's degree in Computer Science or a related field.",
    "Students must be enrolled at an accredited university.",
    "You will work with a team of engineers on production services.",
    "",
])
def test_silence_is_not_evidence(text):
    assert terminal_evidence(text) == "no"


@pytest.mark.parametrize("text", [
    # Graduation WINDOWS that a May-2027 graduate falls before. These read as
    # acceptance to a naive year match -- an early version of the detector
    # keyed on "graduation ... 2027" and flagged every one of them, which is
    # backwards: they all require graduating AFTER the internship.
    "a related field graduating December 2027 and beyond",
    "expected graduation date of December 2027 - June 2028",
    "The intern program is open to students graduating December 2027 - June 2028",
    "Must graduate December 2027 or later. Must continue enrollment in degree program.",
    "You will graduate in fall 2027 or spring 2028 with a degree in Computer Science",
    "Graduation date of December 2027 or May/June 2028",
    "Must be graduating in December 2027 or May/June 2028",
    "students graduating in Dec 2027 or by Summer 2028",
    # PepsiCo, caught by the audit as the one outright-wrong flag: a
    # graduation *window* a May-2027 grad falls before, not a welcome.
    "Graduate with bachelor's or master's degree within one (1) year of internship completion",
    # The phrasing the scoring prompt already calls out -- same words as an
    # accepting year list, opposite meaning.
    "Expected graduation date between December 2027 and June 2028",
    "Must be enrolled and returning to school following the internship",
    "Must be actively enrolled in an accredited institution during the duration of the program",
    "Candidates must have at least one semester remaining after the internship",
])
def test_exclusions_veto_the_flag(text):
    assert terminal_evidence(text) == "no"


def test_exclusion_beats_acceptance_when_both_appear():
    """A posting saying both is saying "welcome, but you must still enrol"."""
    both = ("Open to graduating seniors and those who have recently graduated. "
            "All interns must be returning to school following the internship.")
    assert terminal_evidence(both) == "no"


def test_conversion_language_is_not_terminal_evidence():
    """"Converts to full-time" describes what happens after a successful
    internship, which is equally true of a returning-student one. It says
    nothing about whether a graduating senior may apply."""
    assert terminal_evidence(
        "High performers receive a return offer and convert to full-time.") == "no"


def test_upper_bound_needs_an_actual_date():
    """"Graduate by the application deadline" is not a graduation window."""
    assert terminal_evidence("You must graduate by the application deadline") == "no"


def test_missing_description_is_safe():
    assert terminal_evidence(None) == "no"
