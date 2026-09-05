"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config
from applypilot.ats import detect_ats

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict, start_date_override: str = "") -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.

    Args:
        start_date_override: The active resume variant's configured
            start_date (see settings.json resume_variants). The gap between
            graduating and being available isn't a fixed offset across
            variants -- May 2027 grad -> August 2027 start (+3 months), but
            January 2028 grad -> February 2028 start (+1 month) -- so this
            must come from the variant's own config, never computed from
            grad_date with one formula. Falls back to the profile's static
            availability.earliest_start_date only if a variant lookup wasn't
            available at all.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability -- variant-specific start_date takes priority; see docstring
    lines.append(f"Available: {start_date_override or avail.get('earliest_start_date', 'Immediately')}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony: No",
        "Previously Worked Here: No",
        "How Heard: Online Job Board",
    ])

    # EEO/demographics are deliberately NOT included. The screening section
    # instructs the agent to decline to self-identify on every one of them, so
    # sending the real values would transmit the most sensitive category of
    # personal data to the model on every request without ever using it.

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    # Build the list of acceptable cities for hybrid/onsite
    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    # When the accepted area is a blanket phrase ("any city in the United
    # States") rather than a list of cities, the old wording contradicted
    # itself: it accepted "onsite in any US city" and then rejected "onsite in
    # any city outside the list above". Haiku read a Texas role as outside the
    # list and rejected an eligible job. Nationwide gets its own unambiguous
    # wording with no "outside the list" clause to misread.
    blanket = any(
        kw in city_list.lower()
        for kw in ("any city", "anywhere", "united states", "nationwide", "all us", "any us")
    )

    if blanket:
        return """== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- Remote, hybrid, or onsite ANYWHERE IN THE UNITED STATES -> ELIGIBLE. Apply.
  This includes cities far from where the candidate currently lives. Distance,
  relocation, and commute are NOT reasons to reject a US-based role.
- Outside the United States (India, Philippines, Europe, etc.) with no remote
  option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying.
There is no US city that fails this check. Only reject on location for roles
based outside the United States."""

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- "Remote" or "work from anywhere" -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" elsewhere BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" in a city NOT among ({city_list}) with NO remote option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    hourly_floor = comp.get("internship_hourly_floor", "")
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Compute example hourly rates at 3 salary levels
    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    intern_rule = (
        f"INTERNSHIPS AND CO-OPS: the ${floor} annual figure below does NOT apply. "
        f"Intern roles are paid hourly and a normal rate annualises well below "
        f"any full-time floor -- do not reject one on that basis. The floor for "
        f"an internship is ${hourly_floor}/hour. Only stop if the posting states "
        f"a rate clearly below that.\n\n"
    ) if hourly_floor else ""

    return f"""== SALARY (think, don't just copy) ==
{intern_rule}FULL-TIME ROLES: ${floor} {currency} is the FLOOR. Never go below it. But don't always use it either.

0. Is the desired-salary FIELD ITSELF optional (no asterisk, no "required" marker)? -> Leave it blank. Do not volunteer a number just because you have one -- an optional field with a number in it can anchor negotiation against you for no reason. Only proceed to the rules below when the field is required or the posting explicitly asks the question.

Decision tree (once you've confirmed the field is required):
1. Job posting shows a range (e.g. "$120K-$160K")? -> Answer with the MIDPOINT ($140K).
2. Title says Senior, Staff, Lead, Principal, Architect, or level II/III/IV? -> Minimum $110K {currency}. Use midpoint of posted range if higher.
3. {convert_line}
4. No salary info anywhere? -> Use ${floor} {currency}.
5. Asked for a range? -> Give posted midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict, grad_date: str = "", keywords: str = "") -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]

    skills_section = ""
    if keywords.strip():
        skills_section = f"""

== SKILLS FIELD ==
If the application has a skills field pre-populated by resume-parsing, don't
just leave it as-is. These keywords were pulled from THIS job's description
as things that match or could match the candidate: {keywords.strip()}
Add any of these that aren't already listed -- but only if you can honestly
defend it as something the candidate could reasonably claim (already implied
elsewhere on the resume, or a close variant of a listed skill/tool in the
same domain). Skip anything you can't defend that way, even if it's on the
list above -- this list was generated with looser judgment than what belongs
on a submitted application. This is the same field type you already know how
to handle (tag input, combobox, or plain text) -- no new interaction, just
don't leave free value on the table by accepting the pre-filled list as-is."""

    grad_line = ""
    grad_mismatch_section = ""
    if grad_date:
        grad_line = (
            f"  - Expected graduation date / class standing: {grad_date}. This MUST match the "
            f"resume attached to this application -- do not give a different date than what's "
            f"printed on the resume, even if your training data suggests otherwise.\n"
        )
        grad_mismatch_section = f"""

== GRADUATION DATE -- VERIFY, DO NOT GUESS ==
This application uses a resume printed with graduation date {grad_date}. Getting
this wrong is one of the few mistakes that can disqualify the candidate outright,
so treat it as seriously as work authorization -- never paper over a conflict.

Before you finish the Education section, actively check for a conflict:
- A graduation-date or class-standing DROPDOWN whose available options do NOT
  include {grad_date} (e.g. it only offers 2028, 2029, ... with no 2027) is a
  hard signal -- the employer's form expects a different cohort than the resume
  you're carrying.
- A job requirement explicitly stated in the posting ("must graduate between
  X and Y") that excludes {grad_date} is the same kind of signal.

If you find a genuine conflict like this:
1. Do NOT pick the closest wrong option and keep going -- a submitted
   application with a graduation date that contradicts the attached resume is
   worse than no application at all, and finishing the rest of the form first
   only spends more cost on a doomed submission.
2. Stop immediately and output RESULT:FAILED:grad_date_mismatch, with a one-line
   note on what the form actually required (e.g. "dropdown only offered 2028
   and 2029, no 2027 option").
Do not use this for a vague or ambiguous posting -- only for a form field or
explicit requirement that concretely contradicts {grad_date}."""

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location/relocation: lives in {city}, cannot relocate
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
{grad_line}  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only

Skills and tools -> be confident. This candidate is a {target_role} with {years} years experience. If the question asks "Do you have experience with [tool]?" and it's in the same domain (DevOps, backend, ML, cloud, automation), answer YES. Software engineers learn tools fast. Don't sell short.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real person.

EEO/demographics -> "Decline to self-identify" or "Prefer not to say" for everything.
Gender, Race/Ethnicity, Veteran status and Disability status always get this same
answer -- there is nothing to decide. Set them as one consecutive batch and verify
them together with a single browser_find, rather than open/click/verify four times.{grad_mismatch_section}{skills_section}"""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = "Work auth: Answer truthfully from profile."
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    name_rule = f'Name: Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    if not capsolver_key:
        # Without a key the 8.3k-char CapSolver section is dead weight -- 29% of
        # the prompt, resent on every turn of a ~50-turn agentic loop, purely to
        # say the API is unavailable. Emit the manual fallback only.
        return """== CAPTCHA ==
No CAPTCHA solving service is configured, so you cannot solve image or token
CAPTCHAs programmatically. If one appears:
1. Audio challenge: look for an "audio" or "accessibility" button -- often easier.
2. Text or logic puzzles ("What is 3+7?", "type the word"): solve them yourself.
3. Anything else -> RESULT:CAPTCHA. Do not loop.
Note that invisible CAPTCHAs (reCAPTCHA v3, Turnstile) show no widget but can
silently block a submit. If a form submits with no error and no confirmation,
suspect one and report RESULT:CAPTCHA rather than retrying indefinitely."""

    return f"""== CAPTCHA ==
You solve CAPTCHAs via the CapSolver REST API. No browser extension. You control the entire flow.
API key: {capsolver_key or 'NOT CONFIGURED — skip to MANUAL FALLBACK for all CAPTCHAs'}
API base: https://api.capsolver.com

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE (createTask -> poll -> inject) with the CapSolver API
3. ONLY go to MANUAL FALLBACK if CapSolver returns errorId > 0
Do NOT skip the API call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the API first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{{{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{{{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}}}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 2. Cloudflare Turnstile
  if (!r.type) {{{{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{{{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{{{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}}}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{{{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{{{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{{{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}}}
    }}}}
  }}}}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{{{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{{{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{{{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{{{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{{{
    const el = document.querySelector('[data-pkey]');
    if (el) {{{{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}}}
  }}}}
  if (r.type) {{{{ r.url = url; return r; }}}}
  return null;
}}}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Three steps: createTask -> poll -> inject. Do each as a separate browser_evaluate call.

STEP 1 -- CREATE TASK (copy this exactly, fill in the 3 placeholders):
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/createTask', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      task: {{{{
        type: 'TASK_TYPE',
        websiteURL: 'PAGE_URL',
        websiteKey: 'SITE_KEY'
      }}}}
    }}}})
  }}}});
  return await r.json();
}}}}

TASK_TYPE values (use EXACTLY these strings):
  hcaptcha     -> HCaptchaTaskProxyLess
  recaptchav2  -> ReCaptchaV2TaskProxyLess
  recaptchav3  -> ReCaptchaV3TaskProxyLess
  turnstile    -> AntiTurnstileTaskProxyLess
  funcaptcha   -> FunCaptchaTaskProxyLess

PAGE_URL = the url from detect result. SITE_KEY = the sitekey from detect result.
For recaptchav3: add "pageAction": "submit" to the task object (or the actual action found in page scripts).
For turnstile: add "metadata": {{"action": "...", "cdata": "..."}} if those were in detect result.

Response: {{"errorId": 0, "taskId": "abc123"}} on success.
If errorId > 0 -> CAPTCHA SOLVE failed. Go to MANUAL FALLBACK.

STEP 2 -- POLL (replace TASK_ID with the taskId from step 1):
Loop: browser_wait_for time: 3, then run:
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/getTaskResult', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      taskId: 'TASK_ID'
    }}}})
  }}}});
  return await r.json();
}}}}

- status "processing" -> wait 3s, poll again. Max 10 polls (30s).
- status "ready" -> extract token:
    reCAPTCHA: solution.gRecaptchaResponse
    hCaptcha:  solution.gRecaptchaResponse
    Turnstile: solution.token
- errorId > 0 or 30s timeout -> MANUAL FALLBACK.

STEP 3 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{{{ el.value = token; el.style.display = 'block'; }}}});
  if (window.___grecaptcha_cfg) {{{{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{{{
      const walk = (obj, d) => {{{{
        if (d > 4 || !obj) return;
        for (const k in obj) {{{{
          if (typeof obj[k] === 'function' && k.length < 3) try {{{{ obj[k](token); }}}} catch(e) {{{{}}}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}}}
      }}}};
      walk(clients[key], 0);
    }}}}
  }}}}
  return 'injected';
}}}}

For hCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{{{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For Turnstile:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{{{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For FunCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{{{ window.ArkoseEnforcement.setConfig({{{{data: {{{{blob: token}}}}}}}}) }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
You should ONLY be here if CapSolver createTask returned errorId > 0. If you haven't tried CapSolver yet, GO BACK and try it first.
If CapSolver genuinely failed (errorId > 0):
1. Audio challenge: Look for "audio" or "accessibility" button -> click it for an easier challenge.
2. Text/logic puzzles: Solve them yourself. Think step by step. Common tricks: "All but 9 die" = 9 left. "3 sisters and 4 brothers, how many siblings?" = 7.
3. Simple text captchas ("What is 3+7?", "Type the word") -> solve them.
4. All else fails -> Output RESULT:CAPTCHA."""


def _build_known_quirks_section(ats: str | None) -> str:
    """Build the known-quirks section for one ATS platform, if any exist.

    Empty when nothing has been recorded for this platform yet -- the section
    disappears from the prompt entirely rather than printing a hollow header.
    """
    quirks = config.load_known_quirks(ats)
    if not quirks:
        return ""
    return f"""== KNOWN QUIRKS ON {ats} (verified fixes from past runs) ==
Try the normal approach first. Only reach for one of these if you've verified
the normal approach failed on this specific field (read the value back and it
didn't stick) -- these are fallbacks for a known failure mode, not a default
to apply blindly, since not every posting on this platform hits the same bug.
{quirks}"""


def _prepare_context(job: dict, cover_letter: str | None = None,
                     worker_id: int | None = None,
                     email_override: str | None = None,
                     password_override: str | None = None) -> dict:
    """Resolve documents and build every reusable prompt section for a job.

    Kept separate from the assembler itself so every backend describes the
    same candidate, the same eligibility rules and the same salary/screening
    strategy regardless of which engine ends up driving the browser.

    Args:
        job: Job dict from the database.
        cover_letter: Optional plain-text cover letter override.
        worker_id: When given, documents are copied into a per-worker
            directory instead of the shared ``current`` one, so parallel
            workers don't overwrite each other's uploads.
        email_override: For repeat-testing the same employer's form without
            an ATS remembering a prior run's account. Gmail plus-addressing
            (e.g. ``jomylak+test1@gmail.com``) is a distinct string to every
            signup form but still lands in the same real inbox, so account
            recovery and verification-code lookup keep working unchanged.
            Never used for real applications -- only the test-harness path
            passes this.
        password_override: Same idea, for the account password. Test-harness
            only.

    Returns:
        Dict of resolved paths, text and rendered prompt sections.
    """
    profile = config.load_profile()
    if email_override or password_override:
        # Override at the profile level, not just the local `personal`
        # variable below -- every section builder (_build_profile_summary,
        # _build_hard_rules, etc.) takes the whole `profile` dict and reads
        # profile["personal"] itself, so the override has to live there to
        # actually reach the rendered prompt.
        overrides = {}
        if email_override:
            overrides["email"] = email_override
        if password_override:
            overrides["password"] = password_override
        profile = {**profile, "personal": {**profile["personal"], **overrides}}
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = (config.APPLY_WORKER_DIR / f"worker-{worker_id}" / "documents"
                if worker_id is not None else config.APPLY_WORKER_DIR / "current")
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Resume variant -> graduation date + start date (must match the resume) ---
    resume_variant = job.get("resume_variant") or "default"
    _, _, grad_date, variant_start_date = config.get_resume_variant_paths(resume_variant)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile, start_date_override=variant_start_date)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile, grad_date=grad_date,
                                                 keywords=job.get("keywords") or "")
    hard_rules = _build_hard_rules(profile)
    # The stored application_url is often an aggregator redirect (Jobright,
    # Intern List) that resolves to nothing -- detect_ats correctly returns
    # "aggregator (unresolved)" for it. A prior run that actually navigated to
    # the real ATS may have already resolved and persisted the true platform
    # to this column, which is a far better signal to preload quirks from than
    # re-guessing off a redirect shim. Fresh, never-attempted aggregator jobs
    # still get no preload here -- there is no way to know the real platform
    # before the agent has navigated anywhere.
    stored_ats = job.get("ats")
    if stored_ats and stored_ats != "aggregator (unresolved)":
        detected_ats = stored_ats
    else:
        detected_ats = detect_ats(job.get("application_url") or job.get("url"))
    known_quirks_section = _build_known_quirks_section(detected_ats)

    # Cover letter fallback text
    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    # One password across every employer site -- referenced by both step 5 and
    # the ACCOUNT RECOVERY section, so bind it once.
    STD_PASSWORD = personal.get("password", "")

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # SSO domains the agent cannot sign into (loaded from config/sites.yaml)
    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    # Preferred display name
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    return {
        "profile": profile,
        "search_config": search_config,
        "personal": personal,
        "full_name": full_name,
        "pdf_path": pdf_path,
        "upload_pdf": upload_pdf,
        "dest_dir": dest_dir,
        "cover_letter_text": cover_letter_text,
        "cl_upload_path": cl_upload_path,
        "cl_display": cl_display,
        "grad_date": grad_date,
        "profile_summary": profile_summary,
        "location_check": location_check,
        "salary_section": salary_section,
        "screening_section": screening_section,
        "hard_rules": hard_rules,
        "ats": detected_ats,
        "known_quirks_section": known_quirks_section,
        "phone_digits": phone_digits,
        "blocked_sso": blocked_sso,
        "display_name": display_name,
        "std_password": STD_PASSWORD,
    }


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False,
                 email_override: str | None = None,
                 password_override: str | None = None) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.
        email_override: See ``_prepare_context`` -- test-harness only, lets a
            repeat run on the same employer's form sign up as a fresh
            applicant instead of reusing a prior run's account and its
            already-populated Application Questions page.
        password_override: Same idea, for the account password.

    Returns:
        Complete prompt string for the AI agent.
    """
    ctx = _prepare_context(job, cover_letter=cover_letter, email_override=email_override,
                           password_override=password_override)
    personal = ctx["personal"]
    pdf_path = ctx["pdf_path"]
    cl_upload_path = ctx["cl_upload_path"]
    cl_display = ctx["cl_display"]
    profile_summary = ctx["profile_summary"]
    location_check = ctx["location_check"]
    salary_section = ctx["salary_section"]
    screening_section = ctx["screening_section"]
    hard_rules = ctx["hard_rules"]
    known_quirks_section = ctx["known_quirks_section"]
    phone_digits = ctx["phone_digits"]
    blocked_sso = ctx["blocked_sso"]
    display_name = ctx["display_name"]
    STD_PASSWORD = ctx["std_password"]
    captcha_section = _build_captcha_section()

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows. You are applying for FULL-TIME salaried positions only.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

== TOOL DISCOVERY ==
Before step 1, if any tools you need are not yet loaded, load them all in ONE
ToolSearch call (comma-separated names), not one call per tool. You will need
the Playwright browser tools and, if this job needs account recovery, the
Gmail tools -- load both sets up front rather than discovering them one at a
time as you hit each need.

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
  If clicking Apply opens an application-choice dialog with options such as
  "Autofill with Resume", "Apply Manually", or "Use My Last Application", choose
  "Autofill with Resume" when a resume is available. This is the preferred path
  because it reduces unnecessary entry; after it loads, review and correct every
  autofilled field against the APPLICANT PROFILE and TAILORED RESUME.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Account wall? Do NOT try to log in first. The candidate may not have an
  account on this employer's ATS.
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
  5c. Look for Create account, Register, Sign up, or an equivalent new-applicant option. If available, register with {personal['email']} / {STD_PASSWORD}.
  5d. If registration succeeds, complete any requested email verification, then continue. See ACCOUNT RECOVERY below.
  5e. If registration explicitly says the email/account already exists, or explicitly switches to a sign-in view, and only then, sign in with {personal['email']} / {STD_PASSWORD}.
  5f. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
  5g. If that known-existing account rejects the password, use Forgot password/Reset password. Do not use password reset for a registration error, a generic login error, or an account whose existence was never confirmed.
  5h. If registration is unavailable, registration fails without explicitly saying the account exists, verification cannot be completed, or recovery mail does not arrive -> RESULT:FAILED:login_issue. Do not guess, loop, or try a speculative login.
  5i. After registration or login, run browser_tabs action "list" again. Switch back to the application tab if needed.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields, then fill what's left in ONE pass, not field by field:
   - Snapshot once. List every plain TEXT/number input still empty or wrong on
     this page before touching any of them.
   - Fill every one of those in a SINGLE browser_fill_form call. A page with
     15 text fields costs ONE tool call this way, not 15 -- this is the
     single biggest lever you have for keeping a multi-page form cheap.
     Dropdowns, comboboxes, checkboxes, and date pickers are NOT included in
     that call -- handle each of those individually, see TOOL DISCIPLINE below.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it (the form will auto-submit once the token clears, or you may need to click Submit again). Then check for new tabs (browser_tabs action: "list"). Switch to newest, close old. Snapshot to confirm submission. Look for "thank you" or "application received".
12. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:grad_date_mismatch -- form/posting requires a graduation date that
  conflicts with the attached resume (see GRADUATION DATE section above)
RESULT:FAILED:reason -- any other failure (brief reason)

If a field resisted the normal approach and you had to use a genuinely
different fallback to make it work (not just retrying the same action), and
your RESULT is APPLIED, add one line right before your RESULT line:
QUIRK: <what the normal approach did wrong> -> <what worked instead>
Only report this when you're confident the fix generalizes to this ATS
platform, not just this one employer's form. Skip it for anything you're
unsure actually worked, or that isn't specific to a widget type.

== BROWSER EFFICIENCY ==
- CONTEXT IS THE COST. Every snapshot you take is re-sent to you on every later
  turn for the rest of the run, so one needless full snapshot is paid for dozens
  of times. A measured run spent ~93,000 tokens per turn re-reading context and
  only ~200 on new input. Keep what enters context small:
  * browser_snapshot ONCE when you arrive on a genuinely new page or step.
  * To CHECK a value you just set, use browser_find with that field's label
    (e.g. text: "Country"). It returns only the matching nodes instead of the
    whole page -- the single most effective thing you can do to keep the run
    cheap, and just as reliable for confirming one field.
  * Do NOT re-snapshot the whole form after each field, and do NOT snapshot
    after a navigation unless you actually need new element refs.
  * For an overview of a large form, browser_snapshot accepts a `depth` limit --
    prefer a shallow snapshot over a full one.
  * Don't browser_wait_for as a reflexive precaution after every click -- only
    wait when you actually expect a page transition or async content load
    (after Next/Continue, after an autofill/parse step, after a spinner
    appears). Don't browser_evaluate to re-check something your last snapshot
    or browser_find already showed you -- that's a second read of the same
    state, not a needed one. This is about skipping REDUNDANT checks, not
    skipping verification -- still confirm every value you set; just don't
    confirm the same thing twice.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL plain text fields in ONE browser_fill_form call. Not one at a time.
  Custom comboboxes are the exception -- see TOOL DISCIPLINE below; batching them
  into fill_form leaves them unset and poisons the field for the retry.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently. The detect script finds them even when invisible.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- TOOL DISCIPLINE (this is the difference between a 4-minute run and a 20-minute one):
  Read page state ONLY through browser_snapshot / browser_find. Do NOT use a
  shell/terminal tool to cat, grep, or sed the Playwright MCP's own on-disk
  snapshot files (paths like .playwright-mcp/page-*.yml) -- that is a
  duplicate, slower path to information browser_find already gives you
  directly, and every shell call is its own full turn on top of the browser
  action that already ran. If you want to search a large page for one
  keyword, that is exactly what browser_find is for.
  Set values with browser_type, browser_click, browser_fill_form and
  browser_file_upload. Use browser_evaluate ONLY to READ state you cannot see in
  a snapshot -- never to set a value. Assigning `el.value` and firing a synthetic
  `new Event('input')` does NOT work on custom widgets: SAP SuccessFactors, Oracle
  HCM and Workday commit values through their own event bus, so the field silently
  keeps its old value and you will loop writing JS that never takes effect. Real
  keyboard and mouse events from browser_type/browser_click do commit.
- A FIELD THAT RESISTS: do NOT retry the same action. Retrying is what burns runs.
  Snapshot the element and look at what it actually is, then match the pattern:
  * role="combobox" on an <input> (not a <select>) -> it is a FILTERABLE combobox.
    browser_fill_form and fill() DO NOT WORK on these: they set the value without
    producing keystrokes, so the live-search filter never runs and the list never
    narrows. Worse, the value they leave behind concatenates with what you type
    next ("United StatesUnited States"). So: CLEAR the field first, then
    browser_type(text: value, slowly: true) -- this fires one real keystroke
    per character in a SINGLE tool call, which both triggers the filter and
    avoids the cost of typing digit-by-digit with separate browser_press_key
    calls -- THEN click the matching option from a fresh snapshot. Do not
    scroll the unfiltered list hunting for it.
  * Date field ignores browser_fill_form / fill(), or a value you set doesn't
    read back correctly? Same fix: browser_type(text: "MM/DD/YYYY", slowly:
    true) on the field. Try this BEFORE resorting to individual
    browser_press_key calls for each digit -- one browser_type(slowly) call
    replaces 8-10 press_key turns for the same result, and each turn re-reads
    the whole conversation so far, so this is the single most expensive
    mistake to make on a masked field.
  * A long option list (countries, states, universities) is usually paginated or
    virtualised: the option you want is not in the DOM until you type to filter.
    Clicking blind lands on whatever row happens to be rendered -- this is how a
    Country field ends up set to "Sierra Leone".
  * Any custom widget that is not a native <select>: click to open, click the
    option. Value assignment and fill do nothing on these.
  * SAP SuccessFactors (class names starting rcm*, fd-input, or juic handlers),
    Oracle HCM (oj-*), and Workday all use custom widgets of this kind.
- VERIFY every dropdown after setting it -- with browser_find on that field's
  label, not a full snapshot. An action that "succeeded" often left the field
  unchanged or set it to the wrong option, and a form submitted with the wrong
  country is worse than a failure. Cheap verification is also what stops you
  re-clicking blindly: check once and you know immediately instead of guessing.
  Keep working the field until it holds the right value -- do not give up on it
  and do not move on while it is wrong.
- A required field can appear only AFTER you answer another one (choosing "Other"
  for "how did you hear about this position" reveals a required "Details" box).
  Re-check the form for newly required fields before submitting.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- DATE FIELD PROTOCOL -- classify the widget before you act, don't guess-and-retry:
  Today's date, if a field asks for it: {datetime.now().strftime('%m/%d/%Y')}
  Snapshot or browser_find the field first and match it to ONE of these three
  known patterns -- across real runs, a Workday-style ATS has used all three,
  sometimes for different fields on the SAME application, so don't assume
  yesterday's fix applies to today's field until you've checked:
  1. A single typeable <input> holding a MM/DD/YYYY-style value or placeholder
     -> browser_type(text: "MM/DD/YYYY", slowly: true) directly into it, then
     verify with browser_find. Try this first whenever the field looks like an
     ordinary text input -- it's the fastest path when it works.
  2. Three sibling spinbutton inputs for month/day/year (often
     data-automation-id*="dateSection...", or role="spinbutton") ->
     browser_type(slowly) usually does NOT commit on this widget. Click the
     first (month) segment, then send the digits for month, day, and year with
     browser_press_key -- the widget auto-advances between segments as each
     fills, so explicit Tab presses are usually unnecessary but harmless if
     you add them anyway.
  3. No typeable input at all -- only a calendar icon/button that opens a
     popup with Previous/Next-month navigation and clickable day numbers ->
     do NOT keep trying to type into it, however many times you retry --
     typing will never commit here. The moment you see a calendar popup with
     no focusable date text input beside it, switch immediately to: click the
     calendar icon, click "Next month" (or "Previous month") the number of
     times needed to reach the target month/year, then click the correct day.
  To CHANGE a date already set wrong: for patterns 1 and 2, just re-enter the
  value the same way you set it -- the field or spinbutton accepts new digits
  directly, no separate clear step needed. For pattern 3, reopen the calendar
  and click the correct day again; selecting a new day replaces the old one.
  Verify with browser_find afterward, same as any other field.
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{known_quirks_section}

== ACCOUNT RECOVERY (the same password is used everywhere) ==
The candidate uses ONE password on every employer site: {STD_PASSWORD}
There is never a different password to look up -- if this one is rejected, the
account exists with a password you do not have, and the answer is always to reset it.

Do NOT pre-emptively sign in or open password recovery. Start the application
normally and choose Create account/Register/Sign up when the ATS offers it. Only
branch to login when registration explicitly says the email/account already exists
or the site explicitly switches to a sign-in view. A generic login error is not proof
that an account exists; never use it to justify a speculative login or reset.

A. Registration succeeds -> complete verification if requested, then continue.
B. "Account already exists" / "email already registered" / the form flips to a
  sign-in view during registration -> sign in with {personal['email']} / {STD_PASSWORD}.
C. Password rejected on that confirmed-existing account -> RESET IT. Click "Forgot password" / "Reset password",
   submit {personal['email']}, then get the mail (see C). Set the new password to
   exactly {STD_PASSWORD} if the site allows reuse; if it refuses to accept the old
   password, choose {STD_PASSWORD}2 and say so in your final output so the human
   can record it.
D. Reading the email: use search_emails + read_email. Search the inbox first, then
   ALSO search "in:spam" -- employer ATS mail routinely fails sender authentication
   at the destination and lands in spam. This is expected, not a bug. Reset links
   and codes usually expire in ~10 minutes, so check spam promptly instead of
   repeatedly retrying the inbox. If the mail contains a LINK rather than a code,
   open the link and complete the reset on that page.
E. Signed in -> return to the application. The form often loses uploaded files
   across a sign-in, so RE-CHECK the resume field and re-upload if it is empty.
F. Reset mail never arrives after ~2 minutes, or the reset page errors ->
   RESULT:FAILED:login_issue. Do not loop.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
