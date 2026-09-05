"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

from applypilot.config import RESUME_PATH
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

# Ceiling, not a batch size -- fewer pending jobs than this just uses fewer
# threads. Scoring is pure LLM API calls (no browser, no per-provider
# concurrency limit worth worrying about on a paid OpenRouter tier), so this
# is set by what's reasonable for one machine's outbound connections, not by
# any external constraint.
#
# TEMPORARY: bumped from 8 -> 25 for the current OpenRouter/GLM-5.3-flash
# backfill push (see LLM_URL in .env) -- LLMClient._pace()'s proactive
# rate limiter only fires for Gemini (`_is_gemini` check), so it's a no-op
# on this route and there's nothing else in-process throttling concurrency.
# Revert to 8 once back on Gemini for normal day-to-day flow: Gemini's free
# tier is paced by one global lock enforcing 4.3s between ANY two calls
# regardless of thread count, so a high worker count buys nothing there and
# just means more threads blocked on the same lock.
_SCORE_WORKERS = 25


def _wait_for_connectivity(poll_interval: float = 20.0, max_wait: float = 1800.0) -> bool:
    """Block until a plain HTTPS request succeeds, or `max_wait` elapses.

    Used as a circuit breaker after repeated scoring failures: rather than
    retrying every remaining job in the batch at full per-job cost during a
    real outage, pause once here and resume the batch the moment the network
    is actually back. 1.1.1.1 (Cloudflare) rather than the LLM provider
    itself -- if DNS or the network stack is what's down, hitting a provider
    that also happens to be down would confuse "the internet is down" with
    "that one provider is down", and this only needs to tell those apart
    from "nothing is reachable at all".

    Capped rather than an unconditional `while True` -- an outage that
    outlasts max_wait (e.g. offline for hours, not just a blip) should hand
    control back to run_scoring's caller instead of sitting in this one
    `applypilot run score` process indefinitely with nothing to show for it;
    the hourly/overnight pipeline scripts already retry on their own cadence.
    """
    attempt = 0
    waited = 0.0
    while waited < max_wait:
        attempt += 1
        try:
            httpx.get("https://1.1.1.1", timeout=5.0)
            if attempt > 1:
                log.info("Connectivity restored after %d check(s). Resuming.", attempt)
            return True
        except httpx.TransportError:
            log.warning(
                "No network connectivity (check %d) -- waiting %ds before "
                "resuming scoring.", attempt, poll_interval,
            )
            time.sleep(poll_interval)
            waited += poll_interval
    log.error(
        "Still offline after %.0fs -- giving up on this batch, will retry "
        "on the next scoring pass.", max_wait,
    )
    return False

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9: Perfect skill match. Candidate has direct experience in nearly all required skills and
  qualifications. This is the ceiling for a pure skill-based judgment -- a well-earned perfect
  skill match should always get a 9, don't hold back waiting for something more. (A separate,
  later step promotes a small number of these to 10 based on company reputation -- that decision
  is not yours to make here, so never assign a 10 yourself.)
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
Answer yes in either of these cases:
1. The posting explicitly requires continued enrollment after the internship ends (e.g. "must be currently enrolled and returning to school following the internship", "rising senior" for a non-final-semester role) -- do not infer this from a generic "student" or "currently pursuing degree" requirement that a graduating senior would also satisfy.
2. The posting states a graduation-date window or "not graduating before [date]" requirement that excludes the candidate's earlier graduation date below but is satisfied by the later one. Read the posting's window literally and compare both candidate dates against it -- this is a common phrasing (e.g. "expected graduation date between December 2027 and June 2028", "must graduate on or after December 2027") that is functionally the same requirement as case 1, just expressed as a date range instead of the word "returning".
Answer no only when the candidate's earlier graduation date already satisfies whatever window the posting states, or the posting states no graduation timing requirement at all.

ELIGIBILITY CHECK:
Separately from fit, decide whether the candidate is even allowed to apply. This is a
hard gate, not a preference -- answer "no" ONLY for an explicit, stated disqualifier:
- The role requires a Master's or PhD (in progress or completed).
- The role is restricted to freshmen, sophomores, or first-/second-year students.
- The role is based outside the United States.
- The role requires an active security clearance the candidate does not already hold.
- The role requires THREE OR MORE years of professional/industry software experience,
  where internships and co-ops do not count toward it. A stated requirement of 1 or 2
  years is NOT a disqualifier -- those are routinely soft, and the candidate's
  internship record is a fair argument against them. Judge on the LOWEST number the
  posting will accept: "3-6 years" requires three, but "2-10 years" requires two and is
  therefore fine.

Things that are NOT disqualifiers -- never answer "no" for any of these:
- The candidate is a US citizen, authorized to work without sponsorship. A citizenship
  or work-authorization requirement on its own is fine. Do NOT infer immigration or
  visa status from the candidate's university, name, or anything else on the resume:
  the citizenship stated here is the fact, and nothing else on the resume overrides it.
- ANY graduation-date or class-year requirement, and any target year or season in the
  posting. The pipeline maintains multiple resume variants with different graduation
  dates and automatically switches to whichever one a posting requires, so a stated
  graduation window is always satisfiable and is never a reason to reject. This is
  separate from the freshman/sophomore restriction above, which IS a disqualifier
  because it is about year in program, not graduation timing.
- The role being an internship rather than a new-grad/entry-level role, or vice versa.
  The candidate is eligible for both.

When the posting is ambiguous, answer "unclear" rather than "no". "unclear" is treated
as eligible: a wrong "no" silently costs a real opportunity, while a wrong "yes" only
risks a single application.

COMPANY, LOCATION AND PRESTIGE:
Name the hiring company and the job's location from the posting. Report the location
even when it is stated only in passing -- for many of these postings the description is
the only place it appears at all.

Then rate how well-known and reputable that employer is within the tech industry, 1-10: 9-10 for a household-name tech company or a
top-tier engineering org, 6-8 for a well-regarded public company or a funded, recognised
startup, 4-5 for a solid but little-known mid-market employer, 1-3 for an unknown,
staffing/consultancy, or non-tech-focused employer. Judge the employer only -- this
never affects the fit SCORE.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REQUIRES_RETURNING_STUDENT: [yes or no]
PAY: [the stated pay exactly as written, or "not stated"]
BELOW_FLOOR: [yes, no, or unknown -- "unknown" whenever no pay is stated]
COMPANY: [the hiring company's name, or "unknown"]
JOB_LOCATION: [city and state as stated in the posting, "Remote" if remote, or "unknown"]
PRESTIGE: [1-10]
ELIGIBLE: [yes, no, or unclear]
ELIGIBILITY_REASON: [one short phrase naming the disqualifier; leave empty when eligible]
REASONING: [2-3 sentences explaining the score]"""

# Soft location tiebreaker — never a hard filter. Location-based rejection is
# handled separately (search config accept/reject lists); this only nudges
# ties among otherwise-comparable skill matches. Edit this list to change
# which metros get the nudge.
PREFERRED_METROS = [
    "New York City", "San Francisco Bay Area", "Seattle", "Austin", "Boston",
]

# Hours used to convert a quoted annual or monthly figure to an hourly one.
_HOURS_PER_YEAR = 2080.0
_HOURS_PER_MONTH = _HOURS_PER_YEAR / 12.0

# "$23-$43/hr", "$23.25-$33.75/hr", "$6k-$11k/mon", "$140k-$140k/yr" -- the
# shapes discovery actually stores in `salary`.
_PAY_RANGE_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k?)\s*-\s*"
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k?)\s*/\s*(hr|hour|yr|year|mon|month)",
    re.I,
)

# An hourly rate outside this band is a parse artefact, not a real offer. The
# live table contains "$0-$10000k/yr" (~$4807/hr), which must not be read as a
# genuine range -- treating it as unknown is right, treating it as real would
# make it the single most desirable job on the board.
_MIN_SANE_HOURLY = 1.0
_MAX_SANE_HOURLY = 1000.0


def _float_or_zero(value) -> float:
    """Coerce a profile value to a float, tolerating "" / None / "30" / 30."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_pay_range(salary: str | None) -> tuple[float, float] | None:
    """Parse a discovery `salary` string into an (min, max) HOURLY range.

    Returns None when there's no parseable range -- empty, "N/A", or a figure
    outside any believable band. None means "unknown", which callers must treat
    as neutral rather than as a negative: most postings state no pay at all, so
    penalising absence would systematically favour the minority that disclose.
    """
    if not salary:
        return None

    m = _PAY_RANGE_RE.search(salary)
    if not m:
        return None

    lo_s, lo_k, hi_s, hi_k, unit = m.groups()
    try:
        lo = float(lo_s.replace(",", "")) * (1000.0 if lo_k else 1.0)
        hi = float(hi_s.replace(",", "")) * (1000.0 if hi_k else 1.0)
    except ValueError:
        return None

    unit = unit.lower()
    if unit.startswith("yr") or unit.startswith("year"):
        lo, hi = lo / _HOURS_PER_YEAR, hi / _HOURS_PER_YEAR
    elif unit.startswith("mon"):
        lo, hi = lo / _HOURS_PER_MONTH, hi / _HOURS_PER_MONTH

    if hi < lo:
        lo, hi = hi, lo
    if lo < _MIN_SANE_HOURLY or hi > _MAX_SANE_HOURLY:
        return None

    return lo, hi


def pay_below_floor(salary: str | None, hourly_floor: float) -> str:
    """Whether a posted range tops out below the candidate's hourly floor.

    The test is on the range's MAXIMUM, not its minimum. Twelve of the jobs
    currently on the board are posted at "$23-$43/hr": the bottom of that band
    is under a $30 floor but the top clears it comfortably, and rejecting on
    the minimum would throw all of them away. Only a range whose ceiling is
    below the floor -- "$23-$27/hr" -- genuinely cannot pay enough.

    Returns "yes", "no", or "unknown".
    """
    # Discovery stores an explicitly unpaid role as the word "Unpaid", with no
    # figures for the range parser to find. Left to fall through it would read
    # as "unknown" and clear the gate -- the one case where absence of a number
    # is a definite answer rather than a missing one.
    if salary and "unpaid" in salary.lower():
        return "yes"

    parsed = parse_pay_range(salary)
    if parsed is None or not hourly_floor:
        return "unknown"
    return "yes" if parsed[1] < hourly_floor else "no"


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
    company = ""
    job_location = ""
    prestige = 0
    eligible = "unclear"
    eligibility_reason = ""

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
        elif line.startswith("COMPANY:"):
            val = line.replace("COMPANY:", "").strip()
            company = "" if val.lower() in ("unknown", "n/a", "") else val
        elif line.startswith("JOB_LOCATION:"):
            val = line.replace("JOB_LOCATION:", "").strip()
            job_location = "" if val.lower() in ("unknown", "n/a", "") else val
        elif line.startswith("PRESTIGE:"):
            try:
                prestige = max(1, min(10, int(re.search(r"\d+", line).group())))
            except (AttributeError, ValueError):
                prestige = 0
        # Checked before ELIGIBLE: would be ambiguous -- it isn't ("ELIGIBLE:"
        # and "ELIGIBILITY_REASON:" diverge at the 7th character) but ordering
        # them this way keeps that independent of the exact spellings.
        elif line.startswith("ELIGIBILITY_REASON:"):
            eligibility_reason = line.replace("ELIGIBILITY_REASON:", "").strip()
        elif line.startswith("ELIGIBLE:"):
            val = line.replace("ELIGIBLE:", "").strip().lower()
            # Anything the model didn't say cleanly falls to "unclear", which
            # passes the gate -- see the `eligible` column comment in
            # database.py for why the ambiguous case has to fail open.
            eligible = val if val in ("yes", "no", "unclear") else "unclear"
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {
        "score": score, "keywords": keywords, "reasoning": reasoning,
        "requires_returning_student": requires_returning_student,
        "pay_text": pay_text, "pay_below_floor": below_floor,
        "company": company, "job_location": job_location,
        "company_prestige": prestige,
        "eligible": eligible, "eligibility_reason": eligibility_reason,
    }


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    # `site` is the board the job was found on ("Intern List - SWE"), not the
    # employer -- passing it as COMPANY fed the model a constant string that
    # told it nothing. The company is asked for as an output instead.
    #
    # SALARY comes from the discovery row, and is the only place pay actually
    # lives: 40 of the 121 jobs on the board carry a real range here, while
    # just 3 descriptions mention a figure at all. Leaving it out is why every
    # job previously scored "not stated" / BELOW_FLOOR unknown.
    job_text = (
        f"TITLE: {job['title']}\n"
        f"SOURCE BOARD: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n"
        f"SALARY: {job.get('salary') or 'not stated'}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    # The floors come from the profile so the scorer judges pay against the
    # same numbers the apply stage uses. Internships and full-time roles get
    # different floors -- an intern rate that looks low annualised is normal.
    from applypilot import config as _config
    profile = _config.load_profile()
    comp = profile.get("compensation", {})
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

    # Work authorization stated as a fact from the profile, not asserted in
    # the static rubric. The model only ever sees the resume, which doesn't
    # mention citizenship -- given no positive evidence it inferred the
    # candidate was an international student needing sponsorship (from the
    # university, apparently) and rejected a job on that basis. Sourcing it
    # from the profile makes it evidence rather than a claim the resume seems
    # to contradict.
    auth = profile.get("work_authorization", {})
    permit = auth.get("work_permit_type", "")
    auth_note = (
        f"\n\nCANDIDATE WORK AUTHORIZATION (authoritative -- this is stated fact "
        f"about the candidate, not an inference to re-derive): work permit type "
        f"\"{permit}\"; legally authorized to work: "
        f"{'yes' if auth.get('legally_authorized_to_work') else 'no'}; "
        f"requires visa sponsorship: "
        f"{'yes' if auth.get('require_sponsorship') else 'no'}. "
        f"A posting that excludes candidates needing sponsorship therefore does "
        f"NOT disqualify this candidate. Never infer immigration status from the "
        f"resume's university, coursework, or name."
    ) if permit else ""

    # Concrete dates for the RETURNING STUDENT CHECK above, pulled from the
    # actual configured resume variants rather than hardcoded -- keeps the
    # prompt correct if the variants (or their dates) ever change.
    from applypilot.config import load_settings as _load_settings
    variants = _load_settings().get("resume_variants", {})
    default_grad = variants.get("default", {}).get("grad_date", "")
    later_grad = next(
        (v.get("grad_date") for k, v in variants.items() if k != "default" and v.get("grad_date")),
        "",
    )
    grad_date_note = (
        f"\n\nCANDIDATE GRADUATION DATE OPTIONS (for the RETURNING STUDENT CHECK "
        f"above): the candidate can present as graduating in {default_grad} "
        f"(default) or as a returning student graduating {later_grad} -- use "
        f"these two dates, in this order, when checking whether either satisfies "
        f"a posting's stated graduation window."
    ) if default_grad and later_grad else ""

    messages = [
        {"role": "system", "content": SCORE_PROMPT + pay_note + auth_note + location_note + grad_date_note},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        # 640 was sized for Gemini's original six-then-ten output lines, but
        # reasoning-style free/OpenRouter models (MiMo, GLM-flash, whatever
        # openrouter/free routes to) spend 600-4000+ tokens on hidden
        # chain-of-thought before ever writing the answer, and silently
        # return empty content (finish_reason=length) if the budget runs out
        # mid-thought -- the worst real call measured against GLM-5.3-flash
        # used 4027 reasoning tokens alone. 6000 covers that plus the actual
        # ten-line answer with headroom, while still keeping a worst-case
        # full backlog (every one of ~4500 jobs maxing out the ceiling) under
        # $8 at GLM's per-token price -- comfortably inside a $10 budget.
        # This is a ceiling, not a cost: real spend is set by tokens actually
        # used (observed avg ~$0.0005/job), and this doesn't affect Gemini's
        # spend at all since it never needs anywhere close to this budget.
        response = client.chat(messages, max_tokens=6000, temperature=0.2)
        parsed = _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}",
                "requires_returning_student": "no",
                "pay_text": "", "pay_below_floor": "unknown",
                "company": "", "job_location": "", "company_prestige": 0,
                "eligible": "unclear", "eligibility_reason": ""}

    # Where discovery gave us a real range, arithmetic beats the model's
    # judgement: the floor comparison is exact, and it's the verdict that gates
    # an apply run worth several dollars. Only fall back to what the LLM read
    # out of the description when there's no parseable range to compare.
    deterministic = pay_below_floor(job.get("salary"), _float_or_zero(hourly_floor))
    if deterministic != "unknown":
        parsed["pay_below_floor"] = deterministic
        if not parsed.get("pay_text") or parsed["pay_text"].lower() == "not stated":
            parsed["pay_text"] = job.get("salary") or ""

    # is_terminal_internship is NOT derived here any more -- see
    # compute_terminal_internships(), which derives it from stored columns
    # after scoring. It needs desirability_score, which doesn't exist until
    # compute_desirability() has run, and deriving it here made the flag
    # unfixable without a full LLM re-score.

    # A literal 10 used to mean nothing distinguishable from a 9 -- the
    # rubric described both identically, so the model almost never gave one
    # (a well-known calibration behavior: no signal for what separates the
    # two, so it anchors on the "safe" 9). Rather than ask the model to hold
    # two judgments at once in a single call, this is a deterministic
    # post-hoc rule: 10 = a real 9 (excellent skill match) at a genuinely
    # strong company/opportunity (prestige >= 8, already computed above).
    #
    # The prompt tells the model never to self-assign a 10, but a negative
    # instruction isn't reliably obeyed by every model (confirmed live: GLM
    # occasionally still returns SCORE: 10 directly). Clamp any such score
    # down to 9 first, so this rule is the ONLY path to a 10 regardless of
    # what the model outputs -- otherwise a direct model 10 would just pass
    # through untouched, since this rule only ever promotes 9->10, never
    # demotes an already-10.
    if parsed.get("score", 0) > 9:
        parsed["score"] = 9
    if parsed.get("score") == 9 and parsed.get("company_prestige", 0) >= 8:
        parsed["score"] = 10

    return parsed


# Housing support in an internship posting. Detection only -- deliberately no
# dollar parsing.
#
# An earlier version converted a stated stipend to an effective hourly rate
# and fed it through the pay tier. Measured over 1090 internships, that
# credited 249 but changed the score of only 56, and when it did change it
# was ALWAYS exactly +1.00 -- the pay tier is a step function, so a
# $11.55/hour stipend and a $3.00/hour one produce the same result. The
# precision was thrown away, while the three filters it discarded on were
# arbitrary: 81 jobs sat too far from a tier boundary, 76 stated no parseable
# pay, and 36 were in a preferred metro where _location_desirability returns
# before the pay tier is consulted at all -- so a San Francisco internship
# with a housing stipend got nothing, in the city where it matters most.
#
# A flat bump applied directly to desirability treats all 249 consistently
# and loses no information the old version actually used. Reading no numbers
# also removes the entire class of wage-mistaken-for-stipend bugs: Ramp's
# "monthly rate for this internship is $11,700 USD + housing stipend" simply
# reads as "housing is offered", which is all it ever needed to say.

# Word boundaries are not optional: "data warehousing" contains "housing"
# (PepsiCo). Nor is the benefit word -- "Freddie Mac is a housing finance
# company" is not an offer.
_HOUSING_OFFER = re.compile(
    r"\b(?:housing|lodging|relocation)\s*(?:stipend|allowance|assistance|support|benefits?|package)"
    r"|\b(?:housing|lodging|relocation)\b[^.]{0,25}\b(?:provided|offered|available|covered|reimburs\w+)"
    r"|\b(?:corporate|short[- ]term|temporary|subsidi\w+|free|full|paid)[- ]?housing"
    r"|\bhousing\s+and\s+relocation"
    r"|\bstipend\b", re.I)

_HOUSING_NEGATED = re.compile(
    r"\bno\b[^.]{0,25}\b(corporate )?housing\b"
    r"|housing[^.]{0,25}\bnot\b[^.]{0,20}(provided|available|offered)", re.I)


def offers_housing(description: str | None) -> bool:
    """Does this internship posting offer housing or relocation support?

    Detection only. 250 of 1090 internships promise housing; just 3 state a
    figure, so there is nothing to compute for the other 247 and no reason to
    treat the three differently -- see the note above.
    """
    if not description:
        return False
    if _HOUSING_NEGATED.search(description):
        return False
    return bool(_HOUSING_OFFER.search(description))


def _pay_tier_score(salary: str | None, floor: float) -> float:
    """Score 2/4/6/8 by how far a posted range's top clears the candidate's
    floor, or 5.0 (neutral) if unparseable or no floor is configured.

    Ratio bands were calibrated directly against the candidate's own two real
    anchor points rather than picked arbitrarily: their configured floor is
    $80k, a real return-offer target (NYL, ~$90k) should land as "medium",
    and their own configured salary_range_max ($120k) -- which they used as
    the example of "great, I'd go anywhere" pay -- sits right at the top
    band's threshold. Only used for the "everywhere else" location bucket
    (see _location_desirability): pay never modulates an already-good
    location, it only ever rescues a middling one.
    """
    parsed = parse_pay_range(salary)
    if parsed is None or not floor:
        return 5.0
    ratio = parsed[1] / floor
    if ratio < 1.10:
        return 2.0
    if ratio < 1.25:
        return 4.0
    if ratio < 1.45:
        return 6.0
    return 8.0


def _location_desirability(
    location: str | None, preferred_city: str, salary: str | None, floor: float,
) -> float:
    """Rate a job's location 1-10 for this candidate.

    The candidate lives in Queens, so a role in New York City means no
    relocation at all -- it ranks above the other preferred metros rather than
    merely among them. Remote/a preferred metro is next. Pay never drags
    either of those down. Everywhere else in the US, though, is where pay
    actually matters: good pay should rescue an otherwise-so-so location,
    and bad pay somewhere undesirable should read as genuinely bad, not
    neutral -- see _pay_tier_score.
    """
    blob = (location or "").lower()
    if not blob:
        return 5.0
    if preferred_city.lower() in blob or "new york" in blob or ", ny" in blob:
        return 10.0
    if "remote" in blob or "anywhere" in blob:
        return 7.0
    for metro in PREFERRED_METROS:
        # "San Francisco Bay Area" won't appear verbatim in a location string
        # like "San Francisco, CA" -- match on the city, which is the first
        # two words at most.
        head = " ".join(metro.split()[:2]).lower()
        if head in blob:
            return 7.0
    return _pay_tier_score(salary, floor)


# Phrases that EXPLICITLY admit someone who has finished, or is finishing,
# their degree. Audited against 40 sampled postings: the genuine terminal
# internships nearly all carry one of these, and the phrasing is stable
# enough ("or recently graduated", "or recently completed") that a regex
# beats an LLM here on both cost and consistency.
#
# Deliberately NOT included: "converts to full-time" and "return offer".
# Those describe what happens after a successful internship, which is just
# as true of an ordinary returning-student internship -- they say nothing
# about whether a graduating senior may apply.
TERMINAL_ACCEPT_RE = re.compile(
    r"(recently graduat\w+"
    r"|recently completed (?:an?|your|their|a )?\s*(?:associate|bachelor|master|undergraduate|graduate|college|degree|diploma)"
    r"|(?:or|and) (?:have )?(?:recently )?(?:graduated|completed your degree)"
    r"|graduating seniors?"
    r"|(?:open|available) to graduating"
    r"|within (?:six|6|three|3|nine|9|twelve|12) months of (?:your |their )?graduation"
    r"|final semester"
    r"|have graduated within"
    # An UPPER bound on graduation ("must graduate before December 2027",
    # "open to students graduating by June 2027") is safe to match without
    # any date arithmetic: an earlier graduate satisfies an upper bound by
    # definition, and a role you must graduate *before* is one you do not
    # return to school after. Contrast the LOWER bounds in
    # TERMINAL_EXCLUDE_RE ("December 2027 and beyond", "or later"), which
    # are the opposite and are vetoed there.
    # A year must follow, so "graduate by the application deadline" doesn't
    # count; the gap allows "graduating from undergraduate or Master's
    # programs by June 2027".
    r"|graduat\w*[^.]{0,45}\b(?:before|by)\s+(?:the\s+end\s+of\s+)?(?:\w+\s+)?20\d\d)", re.I)

# Deliberately NOT matched: any pattern keyed on a graduation YEAR. A first
# attempt accepted "expected graduation ... 2027", which then matched
# "graduating December 2027 and beyond" (Mastercard), "December 2027 - June
# 2028" (Adobe, Sierra) and "must graduate December 2027 or later"
# (Honeywell) -- windows that a May-2027 graduate falls BEFORE, i.e. the
# precise opposite of terminal. Date-window arithmetic is not something a
# regex can do safely; requires_returning_student carries that judgement
# instead, and compute_terminal_internships requires it to agree.

# Phrases that EXCLUDE someone who has already graduated. Any of these vetoes
# the flag regardless of what TERMINAL_ACCEPT_RE found, because a posting that
# says both is saying "graduating students welcome, but you must still be
# enrolled" -- and the veto is the operative half.
#
# The first alternative is the PepsiCo case the audit caught: "graduate with a
# bachelor's or master's degree within one (1) year of internship completion"
# is a graduation *window* that a May-2027 grad falls before, not a welcome.
TERMINAL_EXCLUDE_RE = re.compile(
    r"(graduat\w+[^.]{0,60}within (?:one|two|1|2)\s*\(?\d?\)?\s*years?[^.]{0,30}(?:of|after|following)[^.]{0,30}(?:internship|program|completion)"
    r"|must (?:be |remain )?(?:currently )?enrolled[^.]{0,80}(?:following|after) the (?:internship|program)"
    r"|return(?:ing)? to (?:school|campus|classes|studies|university)"
    r"|must graduate (?:on or )?after"
    r"|not (?:be )?graduat\w+[^.]{0,40}(?:before|prior to)"
    r"|at least one (?:semester|term|year)[^.]{0,40}remaining"
    r"|graduat\w*(?: date)?[^.]{0,30}between"
    r"|must continue enrollment"
    r"|graduat\w+[^.]{0,40}(?:and beyond|or later)"
    r"|(?:graduation date of|graduating in|will graduate in)[^.]{0,25}"
    r"(?:dec|fall|winter|aug|sept?|oct|nov)\w*\s*20\d\d)", re.I)


def terminal_evidence(description: str | None) -> str:
    """Does this posting EXPLICITLY welcome someone graduating before the role?

    Returns "yes" only on positive, stated evidence -- never on silence.

    This is the correction for the flag's original failure mode. It used to
    key off requires_returning_student == "no", but the scoring prompt tells
    the model to answer "no" when "the posting states no graduation timing
    requirement at all", and the parser collapses every non-"yes" answer
    (including a malformed one) to "no". So "no" meant "the posting didn't
    mention it", and an audit of 40 flagged jobs found 77.5% were flagged on
    silence alone, 20% had real evidence, and 2.5% were outright wrong.

    Failing open on silence is right for *eligibility* -- a posting that
    states no requirement doesn't exclude anyone. It is wrong as a basis for
    an absolute top-of-queue override, which is a much stronger claim.
    """
    if not description:
        return "no"
    if TERMINAL_EXCLUDE_RE.search(description):
        return "no"
    return "yes" if TERMINAL_ACCEPT_RE.search(description) else "no"


def compute_terminal_internships(conn=None) -> int:
    """Recompute `is_terminal_internship` for every scored job. No LLM calls.

    A terminal internship -- one that explicitly accepts candidates who have
    already graduated -- is functionally a new-grad bridge role, and
    acquire_job() sorts on this flag ahead of any composite score. Because
    that override is absolute, the bar has to be high on every axis:

      1. The posting explicitly says so (terminal_evidence), not merely fails
         to say otherwise.
      2. Strong skill match -- fit_score >= terminal_min_fit.
      3. A job actually worth jumping the queue for --
         desirability_score >= terminal_min_desirability. Without this a
         fit-9 role at an unknown company with desirability 2.0 outranked
         fit-10/desirability-10.0 roles at Google, Mastercard and Adobe.

    Derived from stored columns rather than at parse time so the whole flag
    can be re-tuned or corrected without re-running a single LLM call, and so
    a scoring batch that silently skips the column can be repaired by
    re-running this instead of re-scoring.

    Returns the number of rows whose flag changed.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()
    settings = _config.load_settings()
    min_fit = settings.get("terminal_min_fit", 9)
    min_des = settings.get("terminal_min_desirability", 6.0)

    rows = conn.execute(
        "SELECT url, job_type, fit_score, desirability_score, "
        "       requires_returning_student, full_description, is_terminal_internship FROM jobs "
        "WHERE fit_score IS NOT NULL"
    ).fetchall()

    changed = 0
    for r in rows:
        want = "yes" if (
            r["job_type"] == "internship"
            # Necessary, not sufficient. This is the only field that compares
            # the candidate's graduation dates against a window the posting
            # states; the phrase check below adds the "explicitly says so"
            # requirement it lacks.
            and r["requires_returning_student"] == "no"
            and (r["fit_score"] or 0) >= min_fit
            and (r["desirability_score"] or 0) >= min_des
            and terminal_evidence(r["full_description"]) == "yes"
        ) else "no"
        if r["is_terminal_internship"] != want:
            conn.execute(
                "UPDATE jobs SET is_terminal_internship = ? WHERE url = ?",
                (want, r["url"]),
            )
            changed += 1
    conn.commit()
    log.info("Terminal-internship flags recomputed: %d changed.", changed)
    return changed


def compute_desirability(conn=None) -> int:
    """Recompute `desirability_score` for every scored job. No LLM calls.

    Desirability is "how much does he actually want this job" -- company
    prestige and a combined location+pay judgment -- as opposed to
    `fit_score`, which stays a pure skill match. Keeping the two apart is
    what lets the apply queue rank on a blend of the two without either
    number quietly absorbing the other.

    Location and pay used to be independent weighted components, but that
    let a bad location and bad pay double-count against each other in a
    plain average rather than genuinely interacting -- the candidate wants
    good pay to actively rescue an otherwise-so-so location, not just
    partially offset it. They're folded into one component now (see
    _location_desirability/_pay_tier_score), with pay's old weight rolled
    into location's so the total weight distribution is unchanged.

    Deliberately pure arithmetic over columns that are already stored, so
    re-tuning the weights costs nothing and never requires re-scoring 121 jobs
    through the LLM.

    Returns the number of rows updated.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()

    # load_settings() already merges DEFAULT_SETTINGS, so these keys are
    # present whether or not the user's settings.json mentions them.
    settings = _config.load_settings()
    w_prestige = settings.get("prestige_weight", 0.4)
    w_location = settings.get("location_weight", 0.4) + settings.get("pay_weight", 0.2)
    preferred_city = settings.get("preferred_city", "New York")

    comp = _config.load_profile().get("compensation", {})
    hourly_floor = _float_or_zero(comp.get("internship_hourly_floor"))
    # parse_pay_range() always normalizes a posting's range to an hourly
    # figure (even one stated as an annual salary), so the new-grad floor
    # needs the same conversion to compare on equal terms.
    annual_floor_hourly = _float_or_zero(comp.get("salary_expectation")) / _HOURS_PER_YEAR

    housing_bonus = _float_or_zero(settings.get("housing_bonus", 0.5))

    rows = conn.execute(
        "SELECT url, location, salary, company_prestige, job_type, full_description "
        "FROM jobs WHERE scored_at IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        prestige = row["company_prestige"] or 0
        floor = hourly_floor if row["job_type"] == "internship" else annual_floor_hourly
        location = _location_desirability(row["location"], preferred_city, row["salary"], floor)

        # Drop prestige if we have no signal for it and renormalise, so a job
        # with no company identified is judged on location+pay alone rather
        # than being dragged toward zero by a missing component.
        parts = [(prestige, w_prestige)] if prestige else []
        parts.append((location, w_location))

        total_weight = sum(w for _, w in parts)
        score = (sum(v * w for v, w in parts) / total_weight) if total_weight else 5.0

        # Housing is a flat bump applied outside the weighted components, so
        # it lands on every posting that offers it rather than only those
        # sitting near a pay-tier boundary. Internships only: a new-grad
        # salary already prices in relocation, and it isn't a recurring perk.
        if row["job_type"] == "internship" and offers_housing(row["full_description"]):
            score = min(10.0, score + housing_bonus)

        conn.execute(
            "UPDATE jobs SET desirability_score = ? WHERE url = ?",
            (round(score, 2), row["url"]),
        )
        updated += 1

    conn.commit()
    log.info("Recomputed desirability for %d jobs", updated)
    return updated


def _write_score_results(conn: sqlite3.Connection, results: list[dict]) -> int:
    """Write a batch of score_job() results to the DB. Returns count skipped.

    Called once per chunk from run_scoring() rather than once for an entire
    (possibly huge) batch, so progress is visible and durable incrementally
    instead of all landing -- or all being lost to a crash -- at the very end.
    """
    now = datetime.now(timezone.utc).isoformat()
    skipped = 0
    for r in results:
        # score_job's rubric is 1-10 -- 0 is never a real score, only ever
        # its error sentinel (network blip, truncated response, etc). Never
        # write it, for a previously-scored job OR a brand-new one: writing
        # it to an already-scored job would clobber good data with an error,
        # and writing it to a brand-new job would set fit_score to a non-NULL
        # value that silently drops it out of "pending_score" forever, so a
        # long outage would permanently strand jobs instead of just leaving
        # them to be picked up by the next scoring pass. Skipping the write
        # entirely keeps every failed job eligible for retry next time this
        # runs, with no separate recovery step ever needed.
        if r["score"] == 0:
            if r["_previous_score"]:
                log.warning(
                    "Scoring error on '%s' -- keeping existing score %s instead "
                    "of overwriting with the error sentinel.",
                    r["url"], r["_previous_score"],
                )
            else:
                log.warning(
                    "Scoring error on '%s' -- leaving unscored so it's retried "
                    "on the next scoring pass instead of getting stuck.",
                    r["url"],
                )
            skipped += 1
            continue
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
            "requires_returning_student = ?, pay_text = ?, pay_below_floor = ?, "
            "company = ?, company_prestige = ?, eligible = ?, eligibility_reason = ?, "
            "keywords = ? "
            "WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}", now,
             r.get("requires_returning_student", "no"),
             r.get("pay_text", ""), r.get("pay_below_floor", "unknown"),
             r.get("company", ""), r.get("company_prestige", 0),
             r.get("eligible", "unclear"), r.get("eligibility_reason", ""),
             r.get("keywords", ""),
             r["url"]),
        )

        # Backfill location only where discovery left it blank -- never
        # overwrite a scraped value with a model-read one. NewGrad Jobs
        # extracts title and url only, and enrichment writes just the
        # description/apply-url/ats, so for those 27 jobs the description is
        # the only place a location exists at all. Without this the location
        # weight (joint-largest in desirability) is inert for half the board.
        if r.get("job_location"):
            conn.execute(
                "UPDATE jobs SET location = ? WHERE url = ? "
                "AND (location IS NULL OR location = '')",
                (r["job_location"], r["url"]),
            )
    conn.commit()
    return skipped


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

    log.info("Scoring %d jobs (up to %d concurrently)...", len(jobs), _SCORE_WORKERS)
    t0 = time.time()
    completed = 0
    errors = 0
    total_skipped = 0

    # score_job is pure I/O (an LLM API call, no browser involved) so this
    # parallelizes safely: get_client() returns one shared LLMClient whose
    # pacing lock and fallback-switch flag were already built for concurrent
    # callers (discovery/apply already run with --workers > 1 against the
    # same client).
    #
    # Processed in chunks of _SCORE_WORKERS, written to the DB right after
    # each chunk finishes -- not accumulated into one big list and written
    # only at the very end. That used to mean a 500+ job batch showed zero
    # progress in the DB (and the `scored` count in `applypilot status`)
    # until the *entire* batch finished computing, and a crash or kill
    # partway through lost the whole batch instead of just the in-flight
    # chunk. Chunking the writes also keeps the network-outage circuit
    # breaker meaningful: "how many of the jobs we just tried came back as
    # errors" is a real signal at chunk granularity, not when errors from
    # job 4 and job 400 could get counted as the same incident.
    for chunk_start in range(0, len(jobs), _SCORE_WORKERS):
        chunk = jobs[chunk_start:chunk_start + _SCORE_WORKERS]
        chunk_results: list[dict] = []
        chunk_errors = 0
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            future_to_job = {pool.submit(score_job, resume_text, job): job for job in chunk}
            for future in as_completed(future_to_job):
                job = future_to_job[future]
                result = future.result()
                result["url"] = job["url"]
                # Remember what was already in the DB for this job so an
                # error result (score=0 is score_job's error sentinel, never
                # a real score) can be told apart from a rescore that's
                # actually overwriting something -- see the skip check below.
                result["_previous_score"] = job.get("fit_score")
                completed += 1

                if result["score"] == 0:
                    errors += 1
                    chunk_errors += 1

                chunk_results.append(result)

                log.info(
                    "[%d/%d] score=%d  %s",
                    completed, len(jobs), result["score"], job.get("title", "?")[:60],
                )

        total_skipped += _write_score_results(conn, chunk_results)

        # 2+ errors in one concurrent chunk means this isn't one flaky call
        # -- it's a real outage (score_job already retries transient
        # failures internally). Pause and wait for connectivity before
        # starting the next chunk, instead of burning through the rest of
        # the backlog at full per-job retry cost.
        if chunk_errors >= 2:
            if not _wait_for_connectivity():
                break

    # Desirability is derived from what scoring just wrote (prestige) plus two
    # columns that were already there (location, salary), so it's recomputed
    # once at the end rather than needing a separate command.
    compute_desirability(conn=conn)
    # Must follow compute_desirability -- it reads desirability_score.
    compute_terminal_internships(conn=conn)

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec), %d skipped (kept existing score after an error)",
              completed, elapsed, completed / elapsed if elapsed > 0 else 0, total_skipped)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": completed - errors,
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
