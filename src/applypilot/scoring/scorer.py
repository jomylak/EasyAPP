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
from applypilot.llm import get_scoring_client

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

TERM CHECK:
Identify which academic term this role runs in from its title, dates, or description
(e.g. "Summer 2027" runs roughly May/June-August, "Fall" August-December, "Winter"
December-January, "Spring" January-April/May, "rolling" or "year-round" for co-ops
without a fixed single term). This matters for the RETURNING STUDENT CHECK below --
report your best reading even from indirect evidence (a stated start date, "12-week
internship starting in June", etc). Answer "unclear" only when there is truly nothing
to go on.

This year check applies ONLY to "spring": if a specific year is stated for a Spring
term and it is NOT 2027 -- e.g. a stray "Spring 2026" or "Spring 2028" posting --
answer "unclear" instead of "spring", since the pipeline treats Spring 2027
specifically as a safe, no-conflict term (the candidate's final semester before
graduating) and a different year doesn't get that same guarantee. Do NOT apply this
year check to "fall" or "winter" -- report those normally regardless of what year is
stated. A Fall/Winter term is excluded downstream for reasons that don't depend on
the year: 2026 is already in the past relative to the candidate's search, and 2027
onward is after they expect to already hold a new-grad job, so every fall/winter
should read as "fall"/"winter", never softened to "unclear" by its year.

RETURNING STUDENT CHECK:
The candidate has exactly one true graduation date (given below) and exactly one
resume, printed with that date -- there is no second identity to fall back on. But the
candidate IS a normal, currently-enrolled student up until that date -- a requirement
to be enrolled for the duration of a role that ends at or before the candidate's
graduation is trivially true for them, not a conflict. Answer yes in either of these
cases:
1. The posting explicitly requires continued enrollment PAST the candidate's
   graduation date -- i.e. the role's term (from the TERM CHECK above) runs at or
   after the candidate's graduation, AND the posting requires remaining enrolled
   during or after it (e.g. "must be currently enrolled and returning to school
   following the internship", "rising senior" for a non-final-semester role). A role
   whose entire term falls before the candidate's graduation trivially satisfies any
   such enrollment requirement, so answer no for those regardless of this wording --
   do not infer a conflict from a generic "student" or "currently pursuing degree"
   phrase either; a graduating senior satisfies that too.
2. The posting states a graduation-date window or "not graduating before [date]"
   requirement that the candidate's actual graduation date below does not satisfy.
   Read the posting's window literally. This one is independent of the role's term --
   it is about when the candidate graduates, not when the role happens.
Answer no when the role's term ends at or before the candidate's graduation, when the
candidate's actual graduation date already satisfies whatever window the posting
states, or when the posting states no graduation timing requirement at all.
This is a real eligibility signal, not just a queue-priority input: a "yes"
here means the candidate cannot honestly satisfy this posting, and it is
excluded downstream regardless of how ELIGIBLE below is answered.

TERMINAL EVIDENCE CHECK:
Independent of the two checks above: does this posting itself, in its own words,
affirmatively welcome a candidate who has ALREADY graduated, with nothing requiring
further enrollment? Read for substance, not one fixed phrase -- "recently graduated",
"graduating seniors welcome", "must have attained a Bachelor's degree (not currently
enrolled)", "within one year of graduation", and similar all count; so does language
you have to read past awkward phrasing or a run-on sentence to understand correctly.
A posting whose ONLY requirement is a bare "pursuing a degree in X" states no opinion
either way -- answer no for that; that silence is a separate, softer signal the
pipeline handles on its own, not something to force a yes on here. A role structured
as an ongoing co-op (alternating terms with school, "must have N years of school
completed before this begins") is a NO here even if it never uses exclusionary
wording -- its whole design presumes the candidate returns to school between terms.
A bare "must graduate before/by [some future date]" is NOT enough on its own -- answer
no unless that date is at or before THIS role's own term (from the TERM CHECK above),
which is the only case that actually describes someone who'll already hold the degree
when the role starts. A bound stated for a year or more after the role's own term
(e.g. "must graduate before Summer 2028" on a Summer 2027 posting) describes an
ordinary still-enrolled junior/senior, not a post-grad welcome -- audited real
postings phrased exactly this way turned out to be false positives, not evidence.

ELIGIBILITY CHECK:
Separately from fit, decide whether the candidate is even allowed to apply. This is a
hard gate, not a preference -- answer "no" for an explicit, stated disqualifier. The
common ones, named so you don't have to guess at the bar:
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

That list is not exhaustive -- ANY other explicit, MUST-level requirement the posting
states that the candidate's resume gives no reason to think they meet is also a
disqualifier. Read for substance: membership in a specific religious or civic
organization ("only members of [Church/organization] who are worthy of a temple
recommend qualify"), current or former military/veteran status, a professional license
the candidate doesn't hold, mandatory union membership, and similar all count -- these
are deliberately not listed above one by one, because a posting inventing a new one
tomorrow shouldn't need a prompt update to be caught. A "preferred"/"nice to have"
version of any of these is NOT a disqualifier -- only a stated requirement.

For this category specifically -- an unusual, narrow status the posting requires --
treat the resume's silence as "no", not "unclear". This is the opposite default from
everywhere else in this check, deliberately: the resume not mentioning a graduation
date detail is the POSTING's ambiguity, and silence there is genuinely uninformative.
But the resume not mentioning membership in a specific church, a veteran status, or a
niche license is a fact ABOUT THE CANDIDATE that they would have stated if true --
most candidates aren't members of any one particular such group, so silence is real
evidence of "no", not a coin flip. Only fall back to "unclear" when the posting's own
requirement is itself vague (e.g. it's actually unclear whether the clause is a hard
requirement or a values statement), not when the resume simply doesn't address it.

Things that are NOT disqualifiers -- never answer "no" for any of these:
- The candidate is a US citizen, authorized to work without sponsorship. A citizenship
  or work-authorization requirement on its own is fine. Do NOT infer immigration or
  visa status from the candidate's university, name, or anything else on the resume:
  the citizenship stated here is the fact, and nothing else on the resume overrides it.
- A graduation-date, target-year, or season requirement that the candidate's actual
  graduation date (given in the RETURNING STUDENT CHECK below) already satisfies.
  This is separate from the freshman/sophomore restriction above, which IS a
  disqualifier because it is about year in program, not graduation timing. A
  requirement the candidate's actual graduation date does NOT satisfy is real --
  but it is captured by the RETURNING STUDENT CHECK below, not here, so leave
  ELIGIBLE alone for it; that check drives its own downstream eligibility gate.
- The role being an internship rather than a new-grad/entry-level role, or vice versa.
  The candidate is eligible for both.

When the POSTING's own requirement is ambiguous, answer "unclear" rather than "no" --
"unclear" is treated as eligible: a wrong "no" silently costs a real opportunity, while
a wrong "yes" only risks a single application. This default flips for the narrow-status
disqualifiers just above, where it's the resume's silence that matters, not the
posting's clarity -- see that section for why.

COMPANY, LOCATION AND PRESTIGE:
Name the hiring company and the job's location from the posting. When a COMPANY
line is already given above, that name came from the job board's own structured
data -- echo it back as-is and do not second-guess it from the description. Report the location
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
TERM: [spring, summer, fall, winter, rolling, or unclear -- see TERM CHECK]
REQUIRES_RETURNING_STUDENT: [yes or no]
TERMINAL_EVIDENCE: [yes or no -- see TERMINAL EVIDENCE CHECK]
PAY: [the stated pay exactly as written, or "not stated"]
BELOW_FLOOR: [yes, no, or unknown -- "unknown" whenever no pay is stated]
COMPANY: [the hiring company's name, or "unknown"]
JOB_LOCATION: [city and state as stated in the posting; "Remote" ONLY if fully remote with
  no onsite/hybrid component; "Hybrid" if hybrid; "unknown" if not stated]
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

# Per-lane desirability weights. Kept here as the fallback for the settings
# keys of the same name so the module works against a settings.json written
# before these existed.
DEFAULT_NEW_GRAD_WEIGHTS = {"pay": 0.35, "prestige": 0.35, "location": 0.30}
DEFAULT_INTERNSHIP_WEIGHTS = {"pay": 0.45, "prestige": 0.35, "location": 0.20}

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
    # Sanity-check the TOP of the range only. Every caller (pay_below_floor,
    # _pay_tier_score) compares against `hi`, not `lo` -- a range's minimum
    # being $0 ("unpaid" at the bottom tier, or an unstated floor) is real,
    # if bad, information, not something to discard. Rejecting on `lo` used
    # to throw the whole range away for a posting like "$0-$22/hr", which
    # left BELOW_FLOOR-worthy pay reading as neutral/unknown for
    # desirability purposes even though pay_below_floor (the LLM's own read
    # of the same text) correctly caught it.
    if hi < _MIN_SANE_HOURLY or hi > _MAX_SANE_HOURLY:
        return None
    lo = max(lo, 0.0)

    return lo, hi


def _representative_hourly(parsed: tuple[float, float]) -> float:
    """Collapse a (min, max) pay range to one number for floor/tier comparisons.

    75% of the way from min to max, not the max itself -- a posted range is
    routinely a best-case ceiling few candidates actually land on, so "$20-40/hr"
    reads as ~$35, not $40. Not the midpoint either: postings undersell the
    bottom more than they oversell the top, so weighting toward the top end
    (without just taking it outright) is the better single-number estimate.
    """
    lo, hi = parsed
    return lo + 0.75 * (hi - lo)


def pay_below_floor(salary: str | None, hourly_floor: float) -> str:
    """Whether a posted range's representative pay clears the candidate's
    hourly floor -- see _representative_hourly for why it's not just the max.

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
    return "yes" if _representative_hourly(parsed) < hourly_floor else "no"


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
    term = "unclear"
    requires_returning_student = "no"
    terminal_evidence_llm = "no"
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
        elif line.startswith("TERM:"):
            val = line.replace("TERM:", "").strip().lower()
            term = val if val in ("spring", "summer", "fall", "winter", "rolling") else "unclear"
        elif line.startswith("REQUIRES_RETURNING_STUDENT:"):
            val = line.replace("REQUIRES_RETURNING_STUDENT:", "").strip().lower()
            requires_returning_student = "yes" if val.startswith("yes") else "no"
        elif line.startswith("TERMINAL_EVIDENCE:"):
            val = line.replace("TERMINAL_EVIDENCE:", "").strip().lower()
            terminal_evidence_llm = "yes" if val.startswith("yes") else "no"
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
        "term": term,
        "requires_returning_student": requires_returning_student,
        "terminal_evidence_llm": terminal_evidence_llm,
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
    #
    # Some sources DO know the employer, though: JobSpy returns it as a
    # structured field. Where discovery captured a real name, hand it over
    # rather than paying for the model to re-read it out of the description
    # -- it still rates prestige, which is the part that needs a judgement.
    known_company = (job.get("company") or "").strip()
    company_line = f"COMPANY: {known_company}\n" if known_company else ""
    job_text = (
        f"TITLE: {job['title']}\n"
        f"{company_line}"
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

    # The one true graduation date for the RETURNING STUDENT CHECK above,
    # pulled from settings rather than hardcoded, so the prompt stays correct
    # if the date ever changes. There is exactly one graduation date and one
    # resume -- there is deliberately no second "returning student" identity
    # any more -- see recompute_eligibility_for_grad_date().
    from applypilot.config import get_grad_and_start_dates as _grad_dates
    default_grad, _ = _grad_dates()
    grad_date_note = (
        f"\n\nCANDIDATE GRADUATION DATE (for the RETURNING STUDENT CHECK above): "
        f"{default_grad}. This is the candidate's only true graduation date -- "
        f"compare the posting's stated window against this single date."
    ) if default_grad else ""

    messages = [
        {"role": "system", "content": SCORE_PROMPT + pay_note + auth_note + location_note + grad_date_note},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_scoring_client()
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
        parsed["cost_usd"] = client.get_last_cost_usd()
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}",
                "requires_returning_student": "no",
                "pay_text": "", "pay_below_floor": "unknown",
                "company": "", "job_location": "", "company_prestige": 0,
                "eligible": "unclear", "eligibility_reason": "", "cost_usd": None}

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


# Pay anchors, in hourly terms, mapping compensation to a 1-10 score. These
# are the candidate's own stated numbers, not arbitrary bands: $90k is the
# new-grad target (a real return-offer benchmark), $180k is "outstanding";
# $30/hr is the internship floor and $75/hr is top-of-market for an intern.
# Interpolated linearly between anchors so a $5k difference always moves the
# score -- the old 4-step function returned the same number across a $60k
# spread, which made pay invisible in the ranking.
_PAY_ANCHORS_ANNUAL = [(90_000, 2.0), (120_000, 5.0), (150_000, 7.5), (180_000, 10.0)]
_PAY_ANCHORS_HOURLY = [(30.0, 2.0), (45.0, 5.0), (60.0, 7.5), (75.0, 10.0)]


def _interpolate(x: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation over (input, score) anchor points,
    clamped to the first and last score outside the anchored range."""
    if x <= anchors[0][0]:
        # Below the floor still gets a gradient rather than a flat 1.0, so
        # a $85k offer and a $40k offer are not treated as equally bad.
        lo_x, lo_s = anchors[0]
        return max(1.0, lo_s * (x / lo_x)) if lo_x else 1.0
    for (x0, s0), (x1, s1) in zip(anchors, anchors[1:]):
        if x <= x1:
            return s0 + (s1 - s0) * (x - x0) / (x1 - x0)
    return anchors[-1][1]


def _pay_tier_score(salary: str | None, is_internship: bool) -> float:
    """Rate a posting's pay 1-10 on its own axis, independent of location.

    Returns 5.0 (neutral) when no pay is stated. That is deliberate and it
    matters: 128 of the 470 eligible high-prestige internships carry no pay
    data at all, and scoring those 0 would silently bury exactly the
    postings the candidate most wants to see. An unstated salary is missing
    information, not bad news.

    Internships are judged on the hourly anchors and full-time roles on the
    annual ones -- a $45/hr internship is excellent while a $45/hr
    ($94k) full-time offer is merely acceptable, so one shared curve would
    misjudge one of the two lanes.
    """
    parsed = parse_pay_range(salary)
    if parsed is None:
        return 5.0
    hourly = _representative_hourly(parsed)
    if hourly <= 0:
        return 5.0
    if is_internship:
        return round(min(10.0, _interpolate(hourly, _PAY_ANCHORS_HOURLY)), 2)
    return round(min(10.0, _interpolate(hourly * _HOURS_PER_YEAR, _PAY_ANCHORS_ANNUAL)), 2)


def _location_desirability(location: str | None, preferred_city: str) -> float:
    """Rate a job's location 1-10 for this candidate. Pure location.

    Pay used to be folded in here, which produced a real bug: because the
    preferred city returned a flat 10.0, a NYC posting's pay was discarded
    entirely, and every NYC new-grad role at the same prestige scored an
    identical desirability from $83k to $354k. Pay is its own weighted
    component now (see _pay_tier_score); this function only answers "how
    good is this place to be".

    The candidate lives in Queens, so New York City means no relocation at
    all. Remote and the other preferred metros are next -- they cost either
    nothing or a known, tolerable amount of upheaval. Everywhere else in the
    US is a real move, so it starts low and relies on pay (now a genuinely
    separate term) to carry it.
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
    return 4.0


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
    # Negative lookbehind: "non-graduating senior" (Ingredion's phrasing --
    # sophomore/junior/non-graduating senior, explicitly excluding someone
    # who has already graduated) is the opposite of this pattern's intent,
    # and without the lookbehind the bare substring match fires anyway.
    r"|(?<!non-)(?<!non )graduating seniors?"
    r"|(?:open|available) to graduating"
    r"|within (?:six|6|three|3|nine|9|twelve|12) months of (?:your |their )?graduation"
    r"|final semester"
    r"|have graduated within)", re.I)

# An UPPER bound on graduation ("must graduate before December 2027", "open
# to students graduating by June 2027") used to be its own TERMINAL_ACCEPT_RE
# alternative, on the reasoning that an earlier graduate trivially satisfies
# an upper bound and a role you must graduate *before* is one you don't
# return to school after. Retired 2026-09-08: that reasoning only holds when
# the bound is at or before the ROLE'S OWN TERM, which neither this regex nor
# the scoring prompt ever checked -- a bound stated a full year or more after
# the role (Notion's "must graduate before Summer 2028" on a *Summer 2027*
# posting; Verkada's "graduating by June 2028" on a summer/winter 2027 role)
# describes an ordinary still-enrolled junior/senior, not a post-grad welcome,
# and both real postings this branch had promoted to CONFIRMED (top-of-queue,
# not merely "likely") turned out to be exactly that -- 2 of 2 audited hits
# were false positives, 0 were real. No safe way to compare the bound against
# the role's own term from a regex alone, so this whole class of evidence
# is retired rather than patched.

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
    r"(?:dec|fall|winter|aug|sept?|oct|nov)\w*\s*20\d\d"
    # The Palantir case the second audit caught: "Must be planning on
    # graduating in 2028. This should be your final internship before
    # graduating" -- a bare future graduation year with no month/season
    # token, so the pattern above doesn't fire, and "final internship
    # before graduating" says outright that the candidate hasn't
    # graduated yet. Both mean the same thing the "and beyond"/"or later"
    # alternatives above mean, just phrased without those words.
    r"|planning (?:on |to )?graduat\w+[^.]{0,30}\b20\d\d\b"
    r"|final internship before graduat\w+)", re.I)


# Loose, human-facing recall net -- NOT a detector. Finds any sentence that
# so much as mentions grad-timing/enrollment language, whether or not it
# actually disqualifies anything (TERMINAL_EXCLUDE_RE above is the strict,
# audited veto that does that job). Used to give a human reviewer a
# starting point when skimming an is_terminal_internship_likely posting --
# by construction those rows never trip TERMINAL_EXCLUDE_RE, so this is
# deliberately broader/noisier, not a second copy of the same check.
GRAD_EVIDENCE_SENTENCE_RE = re.compile(
    r"[^.\n]*\b(?:graduat\w*|enroll\w*|return(?:ing)? to school|"
    r"currently pursuing|class of \d{4}|rising (?:senior|junior)|"
    r"degree (?:completion|conferral))\b[^.\n]*[.\n]",
    re.IGNORECASE,
)


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


def recompute_eligibility_for_grad_date(conn=None) -> int:
    """Fold requires_returning_student == 'yes' into the eligibility gate.

    The scoring prompt used to treat any graduation-date requirement as
    "always satisfiable" because the pipeline could print a second resume
    with a later, honestly-held graduation date and switch to it. That
    identity was retired -- there is now exactly one true graduation date
    and one resume -- so a posting requiring continued enrollment past that
    date is no longer satisfiable at all, honestly. `eligible` is the column
    every consumer (acquire_job's SQL gate, the web UI's browse filter)
    already checks, so this folds the signal in there rather than adding a
    second eligibility column nothing else would read.

    Derived from stored columns, no LLM call -- safe to re-run any time
    requires_returning_student values change (a re-score, or a fix to the
    prompt/regex that produces it) without waiting for a fresh scoring pass.

    Only ever tightens eligibility (never loosens it back to 'yes'/'unclear')
    -- if a later run wants to relax this it should re-score, not have this
    function guess at reverting a reason it didn't originally write.

    Returns the number of rows changed.
    """
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT url, eligibility_reason FROM jobs "
        "WHERE requires_returning_student = 'yes' "
        "AND (eligible IS NULL OR eligible != 'no')"
    ).fetchall()

    note = ("Requires continued enrollment / a later graduation date than "
            "the candidate's true one -- no second resume identity to "
            "satisfy this with.")
    changed = 0
    for r in rows:
        reason = r["eligibility_reason"] or ""
        new_reason = f"{reason} {note}".strip() if reason else note
        conn.execute(
            "UPDATE jobs SET eligible = 'no', eligibility_reason = ?, "
            "is_terminal_internship = 'no', is_terminal_internship_likely = 'no' "
            "WHERE url = ?",
            (new_reason, r["url"]),
        )
        changed += 1
    conn.commit()
    log.info("Eligibility tightened for grad-date mismatch: %d changed.", changed)
    return changed


def recompute_eligibility_for_unwanted_term(conn=None) -> int:
    """Exclude internships in a term the candidate won't actually be free for.

    By Fall 2027 the candidate expects to already be working a new-grad job
    (or has one lined up) -- a Fall or Winter 2027 internship isn't a real
    option regardless of how grad-date-safe it is, so this is a genuine
    exclusion, not a ranking demotion. Summer internships (the main terminal
    route, right after May graduation) and Spring internships (the
    remote-spring route, before graduation) are unaffected.

    `term` is the LLM's own read of the posting (TERM CHECK in SCORE_PROMPT),
    NULL/'unclear' for jobs scored before it existed or where the posting
    genuinely doesn't say -- both pass here rather than being excluded on a
    guess, same fail-open reasoning as the `eligible` column generally.

    Derived from stored columns, no LLM call -- safe to re-run any time term
    values change. Only ever tightens eligibility, same as
    recompute_eligibility_for_grad_date, for the same reason: a wrong
    exclusion here should be fixed by re-scoring, not guessed back open.

    Returns the number of rows changed.
    """
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT url, eligibility_reason FROM jobs "
        "WHERE job_type = 'internship' AND term IN ('fall', 'winter') "
        "AND (eligible IS NULL OR eligible != 'no')"
    ).fetchall()

    note = ("Fall/Winter 2027 term -- candidate expects to already be in a "
            "new-grad role by then, not looking for an internship.")
    changed = 0
    for r in rows:
        reason = r["eligibility_reason"] or ""
        new_reason = f"{reason} {note}".strip() if reason else note
        conn.execute(
            "UPDATE jobs SET eligible = 'no', eligibility_reason = ?, "
            "is_terminal_internship = 'no', is_terminal_internship_likely = 'no' "
            "WHERE url = ?",
            (new_reason, r["url"]),
        )
        changed += 1
    conn.commit()
    log.info("Eligibility tightened for unwanted term: %d changed.", changed)
    return changed


def _url_scope_clause(urls: list[str] | None) -> tuple[str, tuple]:
    """SQL fragment + params restricting a query to `urls`, or no-op if None.

    `url` is the jobs table's PRIMARY KEY, so an `IN (...)` lookup here is an
    indexed point-lookup, not a scan -- this is what lets run_scoring() pass
    just the batch it scored instead of paying a full-table cost on every
    recompute call. Callers outside run_scoring (the `applypilot recompute`
    CLI command, tests) pass urls=None to keep the original full-table
    behavior.
    """
    if not urls:
        return "", ()
    placeholders = ",".join("?" for _ in urls)
    return f" AND url IN ({placeholders})", tuple(urls)


def recompute_job_type_from_title(conn=None, urls: list[str] | None = None) -> int:
    """Fix job_type for rows Jobright's own "intern" feed mislabeled.

    _classify_job_type() (discovery/smartextract.py) trusts the source site
    unconditionally at insert time, and title_suggests_new_grad() now catches
    the mismatch for new rows -- but it doesn't reach rows already in the DB
    from before that check existed. Re-derived from the stored title rather
    than at parse time, same rationale as compute_terminal_internships: no
    LLM call, and a batch that ran before this existed gets repaired by
    re-running this instead of re-scraping.

    Only corrects internship -> new_grad; the reverse (a genuine internship
    posting caught by the title heuristic) hasn't been observed and would be
    a title_suggests_new_grad false positive worth fixing there instead.

    Returns the number of rows whose job_type changed.
    """
    from applypilot.discovery.smartextract import title_suggests_new_grad

    if conn is None:
        conn = get_connection()
    scope_sql, scope_params = _url_scope_clause(urls)
    rows = conn.execute(
        "SELECT url, title FROM jobs WHERE job_type = 'internship'" + scope_sql,
        scope_params,
    ).fetchall()

    changed = 0
    for r in rows:
        if title_suggests_new_grad(r["title"]):
            conn.execute(
                "UPDATE jobs SET job_type = 'new_grad' WHERE url = ?",
                (r["url"],),
            )
            changed += 1
    conn.commit()
    log.info("job_type corrected from title: %d changed.", changed)
    return changed


def compute_terminal_internships(conn=None, urls: list[str] | None = None) -> int:
    """Recompute `is_terminal_internship` for every scored job. No LLM calls.

    A terminal internship -- one that affirmatively accepts candidates who
    have already graduated -- is functionally a new-grad bridge role, and
    acquire_job() sorts on this flag ahead of any composite score. Because
    that override is absolute, the bar has to be high on every axis:

      1. Evidence the posting says so, not merely fails to say otherwise.
         Primary source is terminal_evidence_llm -- the scoring prompt's own
         TERMINAL EVIDENCE CHECK, read with actual comprehension rather than
         a fixed phrase list, which is what makes co-ops, "must have attained
         a degree", and garbled/run-on phrasing all readable in the first
         place. TERMINAL_EXCLUDE_RE still runs as an unconditional veto on
         top -- a posting the regex recognizes as explicitly exclusionary
         wins even over an LLM "yes", since that's a cheap safety net against
         a model mistake. Falls back to the regex alone (terminal_evidence())
         only for rows scored before terminal_evidence_llm existed; re-score
         those to get the LLM's read.
      2. Strong skill match -- fit_score >= terminal_min_fit.
      3. A reputable enough employer to be worth jumping the queue for --
         company_prestige >= terminal_min_prestige. Gated on prestige
         directly rather than the blended desirability_score on purpose:
         desirability also folds in location/pay, and the candidate wants
         "is this company legit" judged separately from "is this a good
         deal for me". Below-floor pay is still excluded unconditionally by
         pay_below_floor regardless of this bar.

    Derived from stored columns rather than at parse time so the whole flag
    can be re-tuned or corrected without re-running a single LLM call, and so
    a scoring batch that silently skips a column can be repaired by
    re-running this instead of re-scoring.

    Returns the number of rows whose flag changed.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()
    settings = _config.load_settings()
    min_fit = settings.get("terminal_min_fit", 6)
    min_prestige = settings.get("terminal_min_prestige", 6)

    scope_sql, scope_params = _url_scope_clause(urls)
    rows = conn.execute(
        "SELECT url, job_type, fit_score, company_prestige, pay_below_floor, "
        "       requires_returning_student, terminal_evidence_llm, eligible, "
        "       full_description, is_terminal_internship, terminal_source FROM jobs "
        "WHERE fit_score IS NOT NULL" + scope_sql,
        scope_params,
    ).fetchall()

    changed = 0
    for r in rows:
        llm_ev = r["terminal_evidence_llm"]
        evidence = llm_ev if llm_ev in ("yes", "no") else terminal_evidence(r["full_description"])
        # Hard vetoes: real, per-posting evidence the row is NOT terminal.
        # These override everything, including a prior company_pattern
        # promotion -- a company's general reputation never trumps this
        # specific posting's own pay floor, grad-date conflict, or explicit
        # disqualifier.
        hard_veto = (
            r["job_type"] != "internship"
            or r["pay_below_floor"] == "yes"
            or r["requires_returning_student"] == "yes"
            or r["eligible"] == "no"
            or (r["fit_score"] or 0) < min_fit
            or (r["company_prestige"] or 0) < min_prestige
            or bool(TERMINAL_EXCLUDE_RE.search(r["full_description"] or ""))
        )
        if hard_veto:
            want, source = "no", None
        elif evidence == "yes":
            want, source = "yes", "llm"
        elif r["terminal_source"] == "company_pattern":
            # No new per-posting evidence either way, and no hard veto --
            # preserve a prior company-pattern promotion rather than
            # reverting it every recompute just because this function only
            # ever sees LLM/regex evidence, not the company-level signal
            # compute_company_pattern_terminal() already promoted this row
            # on. That function re-runs right after this one anyway and
            # would just re-promote it, so reverting here would only ever
            # be pointless churn, not a real correction.
            want, source = "yes", "company_pattern"
        else:
            want, source = "no", None
        if r["is_terminal_internship"] != want or (want == "yes" and r["terminal_source"] != source):
            conn.execute(
                "UPDATE jobs SET is_terminal_internship = ?, "
                "terminal_source = ? WHERE url = ?",
                (want, source, r["url"]),
            )
            changed += 1
    conn.commit()
    log.info("Terminal-internship flags recomputed: %d changed.", changed)
    return changed


def compute_remote_spring_internships(conn=None, urls: list[str] | None = None) -> int:
    """Recompute `is_remote_spring_internship` for every scored job.

    A Spring-term internship that's fully remote needs no grad-date evidence
    at all: the candidate is a normal enrolled student for the whole term
    (it ends at or before their May graduation), and remote means no
    relocation/on-campus conflict either. That makes it equally safe to
    auto-send as a confirmed terminal internship, just via a different route
    -- one relies on the posting welcoming an already-graduated candidate,
    this one never needs to raise the graduation question because the
    candidate is still in school throughout. Same fit/prestige bar as
    is_terminal_internship, so the two are comparable priority.

    `location` holds "Remote" only when the scoring prompt judged the role
    fully remote (no onsite/hybrid component) -- see JOB_LOCATION in
    SCORE_PROMPT, written to the `location` column by _write_score_results.
    `term` is the LLM's own read of the posting's academic term (TERM
    CHECK). Both NULL/unclear for jobs scored before these fields existed,
    so this only starts finding matches after a re-score.

    Deliberately independent of is_terminal_internship -- acquire_job()
    combines the two with OR into a single top-priority tier rather than
    stacking them, so a role that happens to satisfy both isn't
    double-boosted (see is_remote_spring_internship's column comment in
    database.py).

    Uses its own bar (remote_spring_min_fit/prestige) rather than
    is_terminal_internship's -- this route to the priority tier needs no
    grad-date evidence at all, so both can stay looser than the terminal
    bars while still keeping out genuine bottom-tier noise.

    Returns the number of rows whose flag changed.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()
    settings = _config.load_settings()
    min_fit = settings.get("remote_spring_min_fit", 5)
    min_prestige = settings.get("remote_spring_min_prestige", 5)

    scope_sql, scope_params = _url_scope_clause(urls)
    rows = conn.execute(
        "SELECT url, job_type, fit_score, company_prestige, term, "
        "       location, pay_below_floor, is_remote_spring_internship FROM jobs "
        "WHERE fit_score IS NOT NULL" + scope_sql,
        scope_params,
    ).fetchall()

    changed = 0
    for r in rows:
        want = "yes" if (
            r["job_type"] == "internship"
            and r["pay_below_floor"] != "yes"
            and r["term"] == "spring"
            and (r["location"] or "").strip().lower() == "remote"
            and (r["fit_score"] or 0) >= min_fit
            and (r["company_prestige"] or 0) >= min_prestige
        ) else "no"
        if r["is_remote_spring_internship"] != want:
            conn.execute(
                "UPDATE jobs SET is_remote_spring_internship = ? WHERE url = ?",
                (want, r["url"]),
            )
            changed += 1
    conn.commit()
    log.info("Remote-spring-internship flags recomputed: %d changed.", changed)
    return changed


def compute_likely_terminal_internships(conn=None, urls: list[str] | None = None) -> int:
    """Recompute `is_terminal_internship_likely` for every scored job.

    Sibling of compute_terminal_internships() for the postings that never
    address post-grad eligibility at all -- no explicit welcome phrase (that
    bucket is already 'yes' on is_terminal_internship) and no explicit
    return-to-school requirement (terminal_evidence() already vetoes those).
    Same fit/prestige bar as the confirmed flag, since this is meant to
    surface "would already be terminal except the posting is silent," not to
    lower the quality bar.

    This is a judgment call, not a fact: a posting that says nothing is, by
    base rate, far more often indifferent to your exact graduation date than
    it is a hidden trap -- postings that actually care tend to say so (that's
    the whole premise TERMINAL_EXCLUDE_RE relies on). Deliberately NOT wired
    into acquire_job()'s queue-jump ordering, unlike is_terminal_internship --
    the confidence here doesn't clear that bar.

    Must run after compute_terminal_internships(), since it treats
    is_terminal_internship == 'no' as "not already confirmed."

    Does NOT trust requires_returning_student alone to mean "silent" --
    that field is an LLM judgment call, not a regex match, and the exact
    failure mode this flag exists to avoid (see terminal_evidence()'s
    docstring: an earlier audit found 77.5% of a requires_returning_student
    -only flag were flagged on silence, not real evidence) can also produce
    a false "no" on a posting that actually states a disqualifying
    graduation requirement in wording TERMINAL_EXCLUDE_RE doesn't happen to
    match. So this still runs TERMINAL_EXCLUDE_RE directly against the
    description as an independent veto, same as terminal_evidence() does
    for the confirmed flag.

    Returns the number of rows whose flag changed.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()
    settings = _config.load_settings()
    min_fit = settings.get("terminal_min_fit", 6)
    min_prestige = settings.get("terminal_min_prestige", 6)

    scope_sql, scope_params = _url_scope_clause(urls)
    rows = conn.execute(
        "SELECT url, job_type, fit_score, company_prestige, pay_below_floor, "
        "       requires_returning_student, is_terminal_internship, "
        "       full_description, is_terminal_internship_likely, terminal_source "
        "FROM jobs WHERE fit_score IS NOT NULL" + scope_sql,
        scope_params,
    ).fetchall()

    changed = 0
    for r in rows:
        # A prior company_pattern_excluded verdict (compute_company_pattern_
        # non_terminal()) is sticky, same as a company_pattern promotion is
        # sticky in compute_terminal_internships() -- without this check,
        # this function runs automatically after every scoring pass and has
        # no idea that verdict exists, so it silently re-derives 'yes' from
        # the raw per-posting criteria alone and undoes the demotion within
        # the next scoring batch. Caught by the very TikTok/Tesla rows the
        # demotion was built for: both kept flipping back to 'yes' with
        # terminal_source still reading 'company_pattern_excluded' every
        # time the live pipeline scored a fresh batch in between recomputes.
        if r["terminal_source"] == "company_pattern_excluded":
            want = "no"
        else:
            want = "yes" if (
                r["job_type"] == "internship"
                and r["pay_below_floor"] != "yes"
                and r["is_terminal_internship"] == "no"
                and r["requires_returning_student"] == "no"
                and (r["fit_score"] or 0) >= min_fit
                and (r["company_prestige"] or 0) >= min_prestige
                and not TERMINAL_EXCLUDE_RE.search(r["full_description"] or "")
            ) else "no"
        if r["is_terminal_internship_likely"] != want:
            conn.execute(
                "UPDATE jobs SET is_terminal_internship_likely = ? WHERE url = ?",
                (want, r["url"]),
            )
            changed += 1
    conn.commit()
    log.info("Likely-terminal-internship flags recomputed: %d changed.", changed)
    return changed


# Below this, a company's evidence is too thin to trust as a pattern -- one
# or two "yes" postings could just as easily be one recruiter's phrasing as
# an actual company-wide policy. Two is the bar because it's the first point
# a repeat isn't a fluke.
_COMPANY_PATTERN_MIN_CONFIRMED = 2


def compute_company_pattern_terminal(conn=None) -> int:
    """Promote is_terminal_internship_likely rows for companies whose OWN
    evidence -- internal (this employer's other postings) or externally
    researched (terminal_company_policy.yaml) -- says post-grad candidates
    are welcome, so a posting's individual silence stops mattering.

    Two independent sources of "this company is safe":
      1. Internal: >= _COMPANY_PATTERN_MIN_CONFIRMED postings at this
         employer already read as explicit-yes (is_terminal_internship LLM
         evidence, not this function), AND zero of its internship postings
         anywhere require continued enrollment (requires_returning_student
         == 'yes'). One contradicting posting is enough to disqualify the
         whole company -- see TikTok/Booz Allen/IBM in the data this was
         built against, all high-"likely" companies that also have real
         requires_returning_student='yes' postings, meaning the silent ones
         are genuinely ambiguous, not silently-fine.
      2. Researched: terminal_company_policy.yaml's accepts_post_grad: true,
         from one-time external research (official FAQ, anecdotal reports).
         Only ever adds -- accepts_post_grad: false is informational, not an
         active exclusion; a row already excluded from the 'likely' bucket
         (requires_returning_student, TERMINAL_EXCLUDE_RE, pay floor, fit/
         prestige bars) stays excluded regardless of company reputation.

    Only ever moves is_terminal_internship_likely='yes' rows to
    is_terminal_internship='yes' -- never touches a row already 'no' from
    real per-posting evidence (a requires_returning_student mismatch,
    TERMINAL_EXCLUDE_RE, or an explicit LLM "no"), since the 'likely' bucket
    by construction excludes all of those already.

    Must run after compute_terminal_internships() and
    compute_likely_terminal_internships(). A grad_date_mismatch discovered
    later during an actual apply attempt still resets is_terminal_internship
    back to 'no' regardless of terminal_source -- see
    _clear_terminal_flags_on_grad_date_mismatch() in apply/launcher.py.

    Returns the number of rows changed.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()

    stats = conn.execute("""
        SELECT company,
               SUM(CASE WHEN is_terminal_internship = 'yes' THEN 1 ELSE 0 END) AS confirmed,
               SUM(CASE WHEN requires_returning_student = 'yes' THEN 1 ELSE 0 END) AS requires_return
        FROM jobs
        WHERE job_type = 'internship' AND company IS NOT NULL AND company != ''
        GROUP BY company
    """).fetchall()
    internal_safe = {
        r["company"] for r in stats
        if r["confirmed"] >= _COMPANY_PATTERN_MIN_CONFIRMED and r["requires_return"] == 0
    }

    researched = _config.load_terminal_company_policy().get("companies", {}) or {}
    researched_safe_substrings = [
        key.lower() for key, entry in researched.items()
        if (entry or {}).get("accepts_post_grad") is True
    ]

    rows = conn.execute(
        "SELECT url, company FROM jobs WHERE is_terminal_internship_likely = 'yes' "
        "AND is_terminal_internship != 'yes'"
    ).fetchall()

    changed = 0
    for r in rows:
        company = r["company"] or ""
        company_lower = company.lower()
        safe = company in internal_safe or any(s in company_lower for s in researched_safe_substrings)
        if safe:
            # Clear the 'likely' flag too -- once promoted this is confirmed,
            # not still-just-likely, and leaving both 'yes' would double-list
            # the row in a "likely, needs review" view even though it no
            # longer needs one.
            conn.execute(
                "UPDATE jobs SET is_terminal_internship = 'yes', "
                "is_terminal_internship_likely = 'no', "
                "terminal_source = 'company_pattern' WHERE url = ?",
                (r["url"],),
            )
            changed += 1
    conn.commit()
    log.info("Company-pattern terminal promotions: %d changed.", changed)
    return changed


# Below this, a company's requires_returning_student='yes' evidence is too
# thin to trust as a program-wide pattern -- same reasoning as
# _COMPANY_PATTERN_MIN_CONFIRMED, just for the opposite conclusion. One
# enrollment-gated posting could be a single recruiter's phrasing; two is the
# first point it reads as the company's own house style, not a fluke.
_COMPANY_PATTERN_MIN_REQUIRES_RETURN = 2


def compute_company_pattern_non_terminal(conn=None) -> int:
    """Demote is_terminal_internship_likely rows for companies whose OWN
    evidence says post-grad candidates are NOT welcome, so a posting's
    individual silence stops earning the benefit of the doubt.

    Mirror image of compute_company_pattern_terminal() above -- same two
    evidence sources, opposite conclusion:
      1. Internal: >= _COMPANY_PATTERN_MIN_REQUIRES_RETURN of this employer's
         OTHER internship postings already require continued enrollment
         (requires_returning_student == 'yes'). A company that gates some of
         its own postings on enrollment isn't one where a differently-worded,
         silent posting can be assumed to be an oversight in the candidate's
         favor.
      2. Researched: terminal_company_policy.yaml's accepts_post_grad: false,
         from one-time external research (official FAQ, anecdotal reports).
         Most researched companies land here -- most internship programs
         genuinely are enrollment-gated by design (see that file's header).

    Only ever moves is_terminal_internship_likely='yes' rows to 'no'. Never
    touches is_terminal_internship -- a posting with real per-posting
    positive evidence (explicit "recently graduated OK" language caught by
    compute_terminal_internships) is never in the 'likely' bucket to begin
    with (that bucket requires is_terminal_internship == 'no'), so company-
    level evidence here can never override or hide a confirmed-terminal
    posting at the same company. IBM and TikTok both have real confirmed
    terminal internships coexisting with a 'false' company-policy verdict --
    that's expected, not a contradiction: the verdict says "silence at this
    company isn't safe," not "nothing here is ever terminal."

    Must run after compute_terminal_internships() and
    compute_likely_terminal_internships(), same as
    compute_company_pattern_terminal(). Order relative to that function
    doesn't matter -- the two operate on disjoint company sets (an
    accepts_post_grad entry is either true or false, never both, and a
    company can't simultaneously clear the >= 2 confirmed-yes/zero-
    requires-return bar above AND the >= 2 requires-return bar here).

    Returns the number of rows changed.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()

    stats = conn.execute("""
        SELECT company,
               SUM(CASE WHEN requires_returning_student = 'yes' THEN 1 ELSE 0 END) AS requires_return
        FROM jobs
        WHERE job_type = 'internship' AND company IS NOT NULL AND company != ''
        GROUP BY company
    """).fetchall()
    internal_unsafe = {
        r["company"] for r in stats
        if r["requires_return"] >= _COMPANY_PATTERN_MIN_REQUIRES_RETURN
    }

    researched = _config.load_terminal_company_policy().get("companies", {}) or {}
    researched_unsafe_substrings = [
        key.lower() for key, entry in researched.items()
        if (entry or {}).get("accepts_post_grad") is False
    ]

    rows = conn.execute(
        "SELECT url, company FROM jobs WHERE is_terminal_internship_likely = 'yes' "
        "AND is_terminal_internship != 'yes'"
    ).fetchall()

    changed = 0
    for r in rows:
        company = r["company"] or ""
        company_lower = company.lower()
        unsafe = company in internal_unsafe or any(s in company_lower for s in researched_unsafe_substrings)
        if unsafe:
            conn.execute(
                "UPDATE jobs SET is_terminal_internship_likely = 'no', "
                "terminal_source = 'company_pattern_excluded' WHERE url = ?",
                (r["url"],),
            )
            changed += 1
    conn.commit()
    log.info("Company-pattern terminal exclusions: %d changed.", changed)
    return changed


def compute_terminal_evidence_hints(conn=None) -> int:
    """Tag every is_terminal_internship_likely='yes' row 'silent' or
    'mentions_enrollment', purely to help a human skim the likely-terminal
    review list faster -- NOT a ranking, filtering, or apply-queue input.

    Uses GRAD_EVIDENCE_SENTENCE_RE, the same loose recall net the module
    already documents as "NOT a detector" -- deliberately not repurposed
    into an automatic exclusion. A "currently pursuing a degree in X" /
    "currently enrolled" mention is universal internship-posting boilerplate
    (it's how a posting describes its typical candidate, not a stated
    requirement), and the scoring prompt already treats it as non-
    disqualifying on purpose -- a graduating senior satisfies it too. Auto-
    demoting on it would reintroduce exactly the failure mode
    is_terminal_internship_likely was built to fix: an earlier, cruder
    version of this flag was audited at 77.5% false-flagged on silence
    alone (see compute_likely_terminal_internships' docstring). So this is a
    label, not a gate.

    Must run after compute_company_pattern_non_terminal(), since a row that
    function just demoted out of 'likely' should not still carry a hint.
    Clears the hint (sets NULL) on any row that isn't currently 'likely',
    so a promoted/demoted row doesn't carry a stale tag.

    Only regex-scans rows that don't already have a hint. This runs on
    every scoring pass now (see run_scoring), including a batch of one job,
    and a job's full_description never changes once enrichment writes it --
    so an existing hint is never stale, and re-scanning the whole likely
    bucket's description text on every call (a real cost: ~6.5s measured
    against a few hundred rows once regex overhead is added up) would be
    pure waste on every call after the first. Only a row newly entering the
    'likely' bucket since the last pass needs a fresh regex scan.

    Returns the number of rows whose hint changed.
    """
    if conn is None:
        conn = get_connection()

    rows = conn.execute(
        "SELECT url, full_description FROM jobs "
        "WHERE is_terminal_internship_likely = 'yes' AND terminal_evidence_hint IS NULL"
    ).fetchall()

    changed = 0
    for r in rows:
        want = "mentions_enrollment" if GRAD_EVIDENCE_SENTENCE_RE.search(r["full_description"] or "") else "silent"
        conn.execute(
            "UPDATE jobs SET terminal_evidence_hint = ? WHERE url = ?",
            (want, r["url"]),
        )
        changed += 1

    stale = conn.execute(
        "UPDATE jobs SET terminal_evidence_hint = NULL "
        "WHERE terminal_evidence_hint IS NOT NULL AND is_terminal_internship_likely != 'yes'"
    )
    changed += stale.rowcount
    conn.commit()
    log.info("Terminal-evidence hints recomputed: %d changed.", changed)
    return changed


def compute_company_tiers(conn=None) -> int:
    """Stamp every scored job with 'tier1', 'adjacent', or NULL.

    Pure string matching over the stored `company` column plus the prestige
    floor -- no LLM call, so re-running this after editing the company lists
    is free.

    Matching is on config.normalize_company()'s output, and a posting matches
    when either normalized name is a whole-word prefix of the other. That
    two-way rule is what makes both "Meta Platforms, Inc." -> "meta" and
    "Jane Street" -> "Jane Street Capital" land, while the word boundary is
    what stops "Block" from swallowing "Blockchain Widgets".

    Returns the number of rows updated.
    """
    from applypilot import config as _config

    if conn is None:
        conn = get_connection()

    tier1_names, adjacent_names = _config.get_tier_companies()
    tier1 = {_config.normalize_company(n) for n in tier1_names} - {""}
    adjacent = {_config.normalize_company(n) for n in adjacent_names} - {""}
    prestige_floor = _config.TIER_PRESTIGE_FLOOR

    def _matches(name: str, candidates: set[str]) -> bool:
        if not name:
            return False
        if name in candidates:
            return True
        return any(
            name.startswith(c + " ") or c.startswith(name + " ")
            for c in candidates
        )

    rows = conn.execute(
        "SELECT url, company, company_prestige FROM jobs WHERE scored_at IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        name = _config.normalize_company(row["company"])
        if _matches(name, tier1):
            tier = "tier1"
        elif _matches(name, adjacent) or (row["company_prestige"] or 0) >= prestige_floor:
            tier = "adjacent"
        else:
            tier = None
        conn.execute("UPDATE jobs SET company_tier = ? WHERE url = ?", (tier, row["url"]))
        updated += 1

    conn.commit()
    log.info("Recomputed company tiers for %d jobs", updated)
    return updated


def compute_desirability(conn=None, urls: list[str] | None = None) -> int:
    """Recompute `desirability_score` for every scored job. No LLM calls.

    Desirability is "how much does he actually want this job" -- company
    prestige and a combined location+pay judgment -- as opposed to
    `fit_score`, which stays a pure skill match. Keeping the two apart is
    what lets the apply queue rank on a blend of the two without either
    number quietly absorbing the other.

    Three genuinely independent components -- pay, company prestige, and
    location -- each weighted per lane. Pay and location were previously
    folded into a single term, which silently discarded pay whenever the
    location scored well on its own: every NYC new-grad posting at the same
    prestige came out with an identical desirability whether it paid $83k or
    $354k. They are separate terms now.

    The weights differ by lane because the candidate's priorities do. For a
    new-grad role, staying in New York is worth nearly as much as the money.
    For an internship it is only one semester, relocation is often paid, and
    good pay makes anywhere workable -- so pay carries more and location
    less.

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
    ng_w = settings.get("new_grad_weights", DEFAULT_NEW_GRAD_WEIGHTS)
    int_w = settings.get("internship_weights", DEFAULT_INTERNSHIP_WEIGHTS)
    preferred_city = settings.get("preferred_city", "New York")

    housing_bonus = _float_or_zero(settings.get("housing_bonus", 0.5))
    remote_bonus = _float_or_zero(settings.get("remote_bonus", 0.5))

    scope_sql, scope_params = _url_scope_clause(urls)
    rows = conn.execute(
        "SELECT url, location, salary, company_prestige, job_type, full_description "
        "FROM jobs WHERE scored_at IS NOT NULL" + scope_sql,
        scope_params,
    ).fetchall()

    updated = 0
    for row in rows:
        is_internship = row["job_type"] == "internship"
        weights = int_w if is_internship else ng_w

        prestige = row["company_prestige"] or 0
        location = _location_desirability(row["location"], preferred_city)
        pay = _pay_tier_score(row["salary"], is_internship)

        # Drop prestige if we have no signal for it and renormalise, so a job
        # with no company identified is judged on pay+location alone rather
        # than being dragged toward zero by a missing component. Pay does not
        # need the same treatment -- _pay_tier_score already returns a
        # neutral 5.0 when a posting states no salary.
        parts = [(pay, weights.get("pay", 0.35)),
                 (location, weights.get("location", 0.30))]
        if prestige:
            parts.append((prestige, weights.get("prestige", 0.35)))

        total_weight = sum(w for _, w in parts)
        score = (sum(v * w for v, w in parts) / total_weight) if total_weight else 5.0

        # Housing is a flat bump applied outside the weighted components, so
        # it lands on every posting that offers it rather than only those
        # sitting near a pay-tier boundary. Internships only: a new-grad
        # salary already prices in relocation, and it isn't a recurring perk.
        if row["job_type"] == "internship" and offers_housing(row["full_description"]):
            score = min(10.0, score + housing_bonus)

        # Same flat-bump treatment as housing, and for the same reason: a
        # remote role is worth more to the candidate regardless of where it
        # sits, and that shouldn't only register for roles that happen to
        # land near a location-tier boundary. `location` holds "Remote"
        # only when the scoring prompt judged the role fully remote (no
        # onsite/hybrid component) -- see JOB_LOCATION in SCORE_PROMPT.
        if row["job_type"] == "internship" and (row["location"] or "").strip().lower() == "remote":
            score = min(10.0, score + remote_bonus)

        conn.execute(
            "UPDATE jobs SET desirability_score = ? WHERE url = ?",
            (round(score, 2), row["url"]),
        )
        updated += 1

    conn.commit()
    log.info("Recomputed desirability for %d jobs", updated)
    return updated


def _write_score_results(conn: sqlite3.Connection, results: list[dict]) -> int:
    """Write score_job() results to the DB. Returns count skipped.

    Called once per completed LLM call from run_scoring() (a single-item
    list) rather than once for an entire (possibly huge) batch, so progress
    is visible and durable the instant each job finishes instead of all
    landing -- or all being lost to a crash -- at the very end.
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
            "term = ?, requires_returning_student = ?, terminal_evidence_llm = ?, "
            "pay_text = ?, pay_below_floor = ?, "
            # Never let an "unknown" from the model erase a real name that
            # discovery already captured -- keep the existing value instead.
            "company = COALESCE(NULLIF(?, ''), company), "
            "company_prestige = ?, eligible = ?, eligibility_reason = ?, "
            "keywords = ?, score_cost_usd = ? "
            "WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}", now,
             r.get("term", "unclear"),
             r.get("requires_returning_student", "no"),
             r.get("terminal_evidence_llm", "no"),
             r.get("pay_text", ""), r.get("pay_below_floor", "unknown"),
             r.get("company", ""), r.get("company_prestige", 0),
             r.get("eligible", "unclear"), r.get("eligibility_reason", ""),
             r.get("keywords", ""), r.get("cost_usd"),
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


def run_scoring(limit: int = 0, rescore: bool = False,
                stale_only: bool = False, stale_min_fit: int | None = None,
                stale_min_prestige: int | None = None) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        stale_only: Re-score only rows that were scored before the TERM and
            TERMINAL EVIDENCE checks were added to the prompt -- they can be
            identified exactly, because those rows have a scored_at but a
            NULL `term`. This matters more than it sounds: 87% of the corpus
            predates those checks, which is why only a couple of dozen rows
            are flagged as terminal internships. Without `term`, the
            Spring/Summer gate falls back to reading the title and the
            terminal-internship path (the one that makes a Summer role
            reachable at all after graduating) never fires.
        stale_min_fit: With stale_only, skip rows whose prior fit_score is
            below this bar -- a low-fit row isn't worth spending an LLM call
            on just to pick up the newer TERM/TERMINAL_EVIDENCE fields. A row
            with no prior fit_score at all (never successfully scored) is
            never skipped regardless of this bar.
        stale_min_prestige: With stale_only, also keep rows whose
            company_prestige clears this bar even if fit_score doesn't --
            a prestigious company is worth the rescore on its own, same as
            company_tier being exempted from the fit floor elsewhere.

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if stale_only:
        query = ("SELECT * FROM jobs WHERE full_description IS NOT NULL "
                 "AND scored_at IS NOT NULL AND (term IS NULL OR term = '')")
        if stale_min_fit is not None or stale_min_prestige is not None:
            conds = ["fit_score IS NULL"]
            if stale_min_fit is not None:
                conds.append(f"fit_score >= {stale_min_fit}")
            if stale_min_prestige is not None:
                conds.append(f"company_prestige > {stale_min_prestige}")
            query += f" AND ({' OR '.join(conds)})"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    elif rescore:
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
    # parallelizes safely: get_scoring_client() returns one shared LLMClient
    # (GLM, separate from enrichment/discovery's client) whose pacing lock
    # was already built for concurrent callers.
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

                # Written the moment this one call finishes, not batched
                # until every other in-flight call in this chunk of up to
                # _SCORE_WORKERS also finishes -- a fast call no longer
                # waits behind a slow/retrying sibling before landing in the
                # DB. Concurrency is unchanged (still up to _SCORE_WORKERS
                # requests in flight); only the write timing moved.
                total_skipped += _write_score_results(conn, [result])

                log.info(
                    "[%d/%d] score=%d  %s",
                    completed, len(jobs), result["score"], job.get("title", "?")[:60],
                )

        # 2+ errors in one concurrent chunk means this isn't one flaky call
        # -- it's a real outage (score_job already retries transient
        # failures internally). Pause and wait for connectivity before
        # starting the next chunk, instead of burning through the rest of
        # the backlog at full per-job retry cost.
        if chunk_errors >= 2:
            if not _wait_for_connectivity():
                break

    # Every job actually scored in this call (including ones _write_score_
    # results skipped as an error, harmlessly -- their derived columns are
    # untouched either way since fit_score/scored_at were never written for
    # them). Restricting the recompute chain below to just these rows turns
    # each of them from an O(whole table) scan into an O(batch) indexed
    # lookup on the `url` primary key -- the fixed ~3.7s/call tax these were
    # costing (measured against ~6-7k rows) was paid in full on every batch
    # regardless of size, including a batch of one job. A separate full
    # recompute (all rows, urls=None) is still available via `applypilot
    # recompute` for after a settings/prompt change that needs every row
    # re-derived, not just the ones just scored.
    batch_urls = [job["url"] for job in jobs]

    # Desirability is derived from what scoring just wrote (prestige) plus two
    # columns that were already there (location, salary), so it's recomputed
    # once at the end rather than needing a separate command.
    compute_desirability(conn=conn, urls=batch_urls)
    # Must precede the terminal computations below -- both filter on
    # job_type == 'internship'.
    recompute_job_type_from_title(conn=conn, urls=batch_urls)
    # No ordering dependency on the others; tightens eligibility wherever
    # requires_returning_student says the candidate's one true grad date
    # can't satisfy the posting.
    recompute_eligibility_for_grad_date(conn=conn)
    # Independent of the above -- excludes Fall/Winter 2027 internships on
    # timing grounds, not grad-date grounds.
    recompute_eligibility_for_unwanted_term(conn=conn)
    # Must follow compute_desirability -- it reads desirability_score.
    compute_terminal_internships(conn=conn, urls=batch_urls)
    # Must follow compute_terminal_internships -- it reads is_terminal_internship.
    compute_likely_terminal_internships(conn=conn, urls=batch_urls)
    # Must follow compute_likely_terminal_internships -- both promote/demote
    # out of its 'likely' bucket, and both need to run every scoring pass
    # (not just from a manual `applypilot recompute`): compute_likely_
    # terminal_internships re-derives every scored row's flag from scratch
    # on every call, so a company-pattern verdict from a previous pass would
    # get silently overwritten by the next batch's plain per-posting result
    # if these weren't run again right after it. (This bit a real run: two
    # company_pattern_excluded companies kept flipping back to 'likely'
    # between manual recompute calls because this loop wasn't yet closed.)
    compute_company_pattern_terminal(conn=conn)
    compute_company_pattern_non_terminal(conn=conn)
    # Review-aid label only; must follow the two calls above so a row they
    # just promoted/demoted doesn't carry a stale hint.
    compute_terminal_evidence_hints(conn=conn)
    # Independent of the above -- a separate route to the same priority tier.
    compute_remote_spring_internships(conn=conn, urls=batch_urls)

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
