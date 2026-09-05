"""Resume variant selection: track x graduation year."""

import pytest

from applypilot.apply.launcher import _flip_grad_year
from applypilot.scoring.tailor import _pick_resume_variant

SETTINGS = {
    "default_resume_variant": "swe_2027",
    "resume_variants": {
        f"{t}_{y}": {} for t in ("swe", "aiml", "data") for y in ("2027", "2028")
    },
}


@pytest.mark.parametrize("job,expected", [
    ({"title": "Frontend Engineer Intern"}, "swe_2027"),
    ({"title": "Machine Learning Intern"}, "aiml_2027"),
    ({"title": "Data Engineer Intern"}, "data_2027"),
    ({"title": "Backend Intern", "requires_returning_student": "yes"}, "swe_2028"),
    ({"title": "AI Engineer Intern", "requires_returning_student": "yes"}, "aiml_2028"),
    # The two axes are independent: a returning-student flag must not disturb
    # the track, and an AI title must not disturb the year.
    ({"title": "Data Scientist Intern", "requires_returning_student": "yes"}, "data_2028"),
    ({"title": "Data Scientist Intern", "requires_returning_student": "no"}, "data_2027"),
])
def test_variant_composes_track_and_year(job, expected):
    assert _pick_resume_variant(job, SETTINGS) == expected


def test_unconfigured_variant_falls_back_to_the_default():
    settings = {"default_resume_variant": "swe_2027",
                "resume_variants": {"swe_2027": {}}}
    assert _pick_resume_variant({"title": "AI Intern"}, settings) == "swe_2027"


@pytest.mark.parametrize("variant,expected", [
    ("swe_2027", "swe_2028"),
    ("aiml_2028", "aiml_2027"),
    ("data_2027", "data_2028"),
    # Rows tailored before tracks existed still have to swap.
    ("default", "swe_2028"),
    ("returning_2028", "swe_2027"),
    # Nothing sensible to flip to -- caller must not swap.
    ("nonsense", None),
])
def test_flip_grad_year(variant, expected):
    assert _flip_grad_year(variant) == expected


def test_flip_preserves_the_track():
    """A grad_date_mismatch says nothing about the track being wrong.

    With only two variants, "swap to the other one" happened to flip the year.
    With six it does not -- and swapping an aiml_2028 job to swe_2027 would
    answer a returning-student posting with a May-2027 resume, reintroducing
    the very mismatch the swap exists to fix.
    """
    for track in ("swe", "aiml", "data"):
        assert _flip_grad_year(f"{track}_2027") == f"{track}_2028"
        assert _flip_grad_year(f"{track}_2028") == f"{track}_2027"
