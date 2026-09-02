"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

COMPENSATION CHECK:
Report the pay if the posting states one. Many postings don't -- that is normal and
must not be penalised. Judge it against the floors given below, applying the hourly
floor to internships/co-ops and the annual floor to full-time roles. Convert between
the two at 2080 hours/year when only one is stated. Never lower the fit SCORE because
of pay -- report it separately so the pipeline can decide.

RETURNING STUDENT CHECK:
Some internships require the candidate to be enrolled in school and returning for at least one more semester/term AFTER the internship ends (e.g. "must be currently enrolled and returning to school following the internship", "not graduating before [date]", "rising senior" for a non-final-semester role). Only answer yes if the posting explicitly requires continued enrollment after the internship -- do not infer it from a generic "student" or "currently pursuing degree" requirement that a graduating senior would also satisfy.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REQUIRES_RETURNING_STUDENT: [yes or no]
PAY: [the stated pay exactly as written, or "not stated"]
BELOW_FLOOR: [yes, no, or unknown -- "unknown" whenever no pay is stated]
REASONING: [2-3 sentences explaining the score]"""

# Soft location tiebreaker — never a hard filter. Location-based rejection is
# handled separately (search config accept/reject lists); this only nudges
# ties among otherwise-comparable skill matches. Edit this list to change
# which metros get the nudge.
PREFERRED_METROS = [
    "New York City", "San Francisco Bay Area", "Seattle", "Austin", "Boston",
]


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response
    requires_returning_student = "no"
    pay_text = ""
    below_floor = "unknown"

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REQUIRES_RETURNING_STUDENT:"):
            val = line.replace("REQUIRES_RETURNING_STUDENT:", "").strip().lower()
            requires_returning_student = "yes" if val.startswith("yes") else "no"
        elif line.startswith("PAY:"):
            pay_text = line.replace("PAY:", "").strip()
        elif line.startswith("BELOW_FLOOR:"):
            val = line.replace("BELOW_FLOOR:", "").strip().lower()
            below_floor = val if val in ("yes", "no") else "unknown"
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {
        "score": score, "keywords": keywords, "reasoning": reasoning,
        "requires_returning_student": requires_returning_student,
        "pay_text": pay_text, "pay_below_floor": below_floor,
    }


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    # The floors come from the profile so the scorer judges pay against the
    # same numbers the apply stage uses. Internships and full-time roles get
    # different floors -- an intern rate that looks low annualised is normal.
    from applypilot import config as _config
    comp = _config.load_profile().get("compensation", {})
    annual_floor = comp.get("salary_expectation", "")
    hourly_floor = comp.get("internship_hourly_floor", "")
    currency = comp.get("salary_currency", "USD")
    pay_note = (
        f"\n\nCOMPENSATION FLOORS ({currency}): internships/co-ops "
        f"${hourly_floor}/hour; full-time roles ${annual_floor}/year. "
        f"Use the hourly floor for any internship, even when the posting "
        f"quotes an annual figure."
    ) if (annual_floor or hourly_floor) else ""

    metros = ", ".join(PREFERRED_METROS)
    location_note = (
        f"\n\nCANDIDATE LOCATION NOTE (soft tiebreaker ONLY — never reduce the "
        f"score for this): the candidate has a mild preference for roles in "
        f"{metros}, or fully remote. If the job is based there, and the skills "
        f"match is otherwise close, you may nudge the score up by at most 1 "
        f"point. Do NOT penalize jobs anywhere else in the US for location — "
        f"score those purely on skills/experience fit."
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT + pay_note + location_note},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}",
                "requires_returning_student": "no",
                "pay_text": "", "pay_below_floor": "unknown"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # Write scores to DB
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
            "requires_returning_student = ?, pay_text = ?, pay_below_floor = ? "
            "WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}", now,
             r.get("requires_returning_student", "no"),
             r.get("pay_text", ""), r.get("pay_below_floor", "unknown"),
             r["url"]),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
