"""The company name discovery already captured must survive to the scorer.

JobSpy returns the employer as a structured field. It used to be read off the
row and then dropped from the INSERT, so the scorer paid an LLM to re-read it
out of the description -- and could still come back "unknown".
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from applypilot.scoring import scorer

BASE_JOB = {
    "title": "SWE Intern",
    "site": "Intern List - SWE",
    "location": "NYC",
    "salary": "",
    "full_description": "Build things.",
}

_REPLY = ("SCORE: 7\nKEYWORDS: python\nREQUIRES_RETURNING_STUDENT: no\n"
          "PAY: not stated\nBELOW_FLOOR: unknown\nCOMPANY: unknown\n"
          "JOB_LOCATION: unknown\nPRESTIGE: 5\nELIGIBLE: yes\n"
          "ELIGIBILITY_REASON: \nREASONING: fine.")


def _prompt_for(job) -> str:
    seen = {}

    class FakeClient:
        def chat(self, messages, **kw):
            seen["p"] = "\n".join(m.get("content", "") for m in messages)
            return _REPLY

    with patch.object(scorer, "get_client", lambda *a, **k: FakeClient()):
        scorer.score_job("resume", job)
    return seen["p"]


def test_known_company_is_handed_to_the_model():
    assert "COMPANY: Stripe" in _prompt_for({**BASE_JOB, "company": "Stripe"})


@pytest.mark.parametrize("value", ["", "   ", None])
def test_no_company_line_when_discovery_had_none(value):
    """The board name is not the employer, so nothing is asserted rather than
    feeding the model a placeholder it would treat as fact."""
    prompt = _prompt_for({**BASE_JOB, "company": value})
    header = prompt.split("DESCRIPTION:")[0]
    assert "COMPANY: " not in header.split("TITLE:")[1]


def test_model_is_still_asked_for_the_company_as_output():
    assert "COMPANY: [the hiring company" in _prompt_for({**BASE_JOB, "company": ""})


# ---------------------------------------------------------------------------
# The write-back guard
# ---------------------------------------------------------------------------

def _apply_guard(rows, writes):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?)", rows)
    conn.executemany(
        "UPDATE jobs SET company = COALESCE(NULLIF(?, ''), company) WHERE url = ?",
        writes)
    return dict(conn.execute("SELECT url, company FROM jobs").fetchall())


def test_unknown_from_the_model_does_not_erase_a_real_name():
    out = _apply_guard([("u1", "Stripe")], [("", "u1")])
    assert out["u1"] == "Stripe"


def test_model_still_fills_in_a_blank():
    out = _apply_guard([("u2", "")], [("Acme", "u2")])
    assert out["u2"] == "Acme"
