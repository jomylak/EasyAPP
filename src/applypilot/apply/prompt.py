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

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
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

    # Availability
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")

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
        return f"""== LOCATION CHECK (do this FIRST before any form) ==
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

Decision tree:
1. Job posting shows a range (e.g. "$120K-$160K")? -> Answer with the MIDPOINT ($140K).
2. Title says Senior, Staff, Lead, Principal, Architect, or level II/III/IV? -> Minimum $110K {currency}. Use midpoint of posted range if higher.
3. {convert_line}
4. No salary info anywhere? -> Use ${floor} {currency}.
5. Asked for a range? -> Give posted midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict, grad_date: str = "") -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]

    grad_line = ""
    if grad_date:
        grad_line = (
            f"  - Expected graduation date / class standing: {grad_date}. This MUST match the "
            f"resume attached to this application -- do not give a different date than what's "
            f"printed on the resume, even if your training data suggests otherwise.\n"
        )

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
them together with a single browser_find, rather than open/click/verify four times."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    auth_info = work_auth.get("legally_authorized_to_work", "")
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


def _prepare_context(job: dict, cover_letter: str | None = None,
                     worker_id: int | None = None) -> dict:
    """Resolve documents and build every reusable prompt section for a job.

    Shared by both prompt assemblers so the Claude Code and Skyvern paths
    describe the same candidate, the same eligibility rules and the same
    salary/screening strategy -- only the surrounding tool instructions differ.

    Args:
        job: Job dict from the database.
        cover_letter: Optional plain-text cover letter override.
        worker_id: When given, documents are copied into a per-worker
            directory instead of the shared ``current`` one. The Skyvern
            backend needs this because it serves that directory over HTTP,
            and because parallel workers would otherwise overwrite each
            other's uploads.

    Returns:
        Dict of resolved paths, text and rendered prompt sections.
    """
    profile = config.load_profile()
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

    # --- Resume variant -> graduation date (must match what's on the resume) ---
    resume_variant = job.get("resume_variant") or "default"
    _, _, grad_date = config.get_resume_variant_paths(resume_variant)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile, grad_date=grad_date)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

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
        "phone_digits": phone_digits,
        "blocked_sso": blocked_sso,
        "display_name": display_name,
        "std_password": STD_PASSWORD,
    }


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    ctx = _prepare_context(job, cover_letter=cover_letter)
    profile = ctx["profile"]
    personal = ctx["personal"]
    full_name = ctx["full_name"]
    pdf_path = ctx["pdf_path"]
    cl_upload_path = ctx["cl_upload_path"]
    cl_display = ctx["cl_display"]
    profile_summary = ctx["profile_summary"]
    location_check = ctx["location_check"]
    salary_section = ctx["salary_section"]
    screening_section = ctx["screening_section"]
    hard_rules = ctx["hard_rules"]
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

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Login wall?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Try sign in: {personal['email']} / {STD_PASSWORD}
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
   5e. Sign in failed? Try sign up with the same email and password.
   5f. Need email verification? See ACCOUNT RECOVERY below.
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. All failed? Output RESULT:FAILED:login_issue. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
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
RESULT:FAILED:reason -- any other failure (brief reason)

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
    browser_type the value with slow/character-by-character typing so real key
    events fire, THEN click the matching option from a fresh snapshot. Do not
    scroll the unfiltered list hunting for it.
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
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

== ACCOUNT RECOVERY (the same password is used everywhere) ==
The candidate uses ONE password on every employer site: {STD_PASSWORD}
There is never a different password to look up -- if this one is rejected, the
account exists with a password you do not have, and the answer is always to reset it.

Do NOT pre-emptively sign in. Start the application normally; only branch when the
site tells you an account exists. Checking first costs a login on every application;
reacting costs one only on the few that need it.

A. "Account already exists" / "email already registered" / the form flips to a
   sign-in view -> sign in with {personal['email']} / {STD_PASSWORD}.
B. Password rejected -> RESET IT. Click "Forgot password" / "Reset password",
   submit {personal['email']}, then get the mail (see C). Set the new password to
   exactly {STD_PASSWORD} if the site allows reuse; if it refuses to accept the old
   password, choose {STD_PASSWORD}2 and say so in your final output so the human
   can record it.
C. Reading the email: use search_emails + read_email. Search the inbox first, then
   ALSO search "in:spam" -- employer ATS mail routinely fails sender authentication
   at the destination and lands in spam. This is expected, not a bug. Reset links
   and codes usually expire in ~10 minutes, so check spam promptly instead of
   repeatedly retrying the inbox. If the mail contains a LINK rather than a code,
   open the link and complete the reset on that page.
D. Signed in -> return to the application. The form often loses uploaded files
   across a sign-in, so RE-CHECK the resume field and re-upload if it is empty.
E. Reset mail never arrives after ~2 minutes, or the reset page errors ->
   RESULT:FAILED:login_issue. Do not loop.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt


def build_skyvern_goal(job: dict, tailored_resume: str,
                       resume_url: str,
                       cover_letter_url: str = "",
                       cover_letter_text: str = "",
                       profile_summary: str = "",
                       location_check: str = "",
                       salary_section: str = "",
                       screening_section: str = "",
                       hard_rules: str = "",
                       display_name: str = "",
                       phone_digits: str = "",
                       personal: dict | None = None,
                       dry_run: bool = False) -> str:
    """Build the navigation goal for the Skyvern backend.

    Deliberately different from ``build_prompt``, not just a trimmed copy:

    - No Playwright MCP tool names. ``browser_snapshot``/``browser_click``/
      ``browser_fill_form`` are Claude Code's tools; naming them here would
      describe an API Skyvern does not have and actively mislead the model.
    - No CAPTCHA section. That flow drives the CapSolver REST API by hand and
      has no Skyvern equivalent.
    - No ``RESULT:`` codes. Skyvern reports the outcome through the run's
      ``data_extraction_schema`` instead of by printing a line we regex out.
    - No step-by-step browser mechanics or "when to give up" rules. Skyvern
      has its own action layer and a ``max_steps`` budget.

    What it keeps is everything about *the candidate and the decision rules* --
    profile, eligibility, salary strategy, screening guidance, hard rules --
    so both backends apply as the same person under the same constraints.

    Args:
        resume_url: Loopback URL the tailored resume is served at. Skyvern
            uploads files by downloading them first, so this must be a URL,
            not a path. See ``apply.fileserver``.
        cover_letter_url: Same, for the cover letter PDF (may be empty).

    Returns:
        The navigation goal string.
    """
    personal = personal or {}

    if dry_run:
        submit_instruction = (
            "DRY RUN: fill in every field completely, but do NOT click the final "
            "Submit/Apply button. Once the form is filled and reviewed, stop and "
            "report the result as applied, noting that this was a dry run."
        )
    else:
        submit_instruction = (
            "Before submitting, re-read every field on the page and confirm it matches "
            "the applicant profile and resume -- name, email, phone, location, work "
            "authorization, resume uploaded, cover letter if applicable. Fix anything "
            "wrong or missing first. Then submit, and confirm the submission landed "
            "(a confirmation page, 'thank you', or 'application received')."
        )

    cl_block = cover_letter_text or (
        "None available. Skip if optional. If required, write two factual sentences "
        "drawn from the resume."
    )
    cl_file_line = (
        f"Cover letter PDF (download and upload if a file field asks for one): {cover_letter_url}"
        if cover_letter_url else "Cover letter PDF: none"
    )

    return f"""Apply to this job on behalf of the candidate described below, and submit the application.

== JOB ==
Title: {job['title']}
Company: {job.get('site', 'Unknown')}

== FILES ==
Resume PDF (upload this to any resume/CV file field): {resume_url}
{cl_file_line}

The resume is served over HTTP. When a file upload field asks for a resume or CV,
use that URL. Always upload this resume even if the form already has one attached --
it is tailored to this specific job. This is required; an application submitted
without it does not count as complete.

== APPLICANT PROFILE ==
{profile_summary}

== RESUME TEXT (source of truth for any text field) ==
{tailored_resume}

== COVER LETTER TEXT (paste if a text field asks for one) ==
{cl_block}

{hard_rules}

== NEVER DO THESE (stop and report failure instead) ==
- Never grant camera, microphone, screen sharing, or location permissions -> unsafe_permissions
- Never do video/audio verification, selfie capture, ID photo upload, or biometrics -> unsafe_verification
- Never create a freelancing or contractor marketplace profile (Mercor, Toptal, Upwork,
  Fiverr, Turing). Those are not job applications -> not_a_job_application
- Never agree to hourly/contract rates, availability calendars, or "set your rate" flows.
  This candidate is applying for full-time salaried roles only.
- Never install browser extensions, download executables, or run assessment software.
- Never enter payment details, bank details, or a national ID / SSN / SIN.
- Never sign in through Google, Microsoft, or any other SSO/OAuth provider -> sso_required
- If the page is not actually a job application (profile builder, talent network signup,
  skills marketplace, coding assessment platform) -> not_a_job_application

{location_check}

{salary_section}

{screening_section}

== ACCOUNTS AND LOGINS ==
If the site requires an account on the employer's own system, sign in with
{personal.get('email', 'the profile email')} and the profile password, or register a new
account with the same email. If sign-in and registration both fail, or the site demands
SSO, stop and report login_issue rather than retrying indefinitely.

The candidate uses ONE password on every employer site, the profile password above.
There is never a different one to look up: if it is rejected, the account exists with
a password you do not have, and the answer is to reset it. Do not pre-emptively sign
in -- start the application normally and only branch when the site says an account
exists. Then: sign in with that password; if rejected, use "Forgot password" with the
same email, complete the reset from the email, and set the password back to the same
one. After signing in, re-check the resume field -- forms routinely drop uploaded
files across a sign-in.

If a step asks for an emailed verification code, request the code and enter it -- it is
fetched from the inbox automatically, including from the spam folder, so wait for it
rather than giving up. Codes usually expire in about 10 minutes, so ask for a fresh one
if the first has gone stale. If instead the email contains a "verify"/"confirm" LINK
rather than a code, it is opened for you in the background: wait a few seconds, reload
the page, and continue -- you do not need to find or click the link yourself.

== FILLING THE FORM ==
Set values by typing and clicking as a person would. Do not set fields by running
JavaScript: assigning a value and firing a synthetic input event does not commit on
SAP SuccessFactors, Oracle HCM or Workday widgets, which listen to their own event
bus -- the field silently keeps its old value.

If a field will not take a value, do not repeat the same action -- inspect the
element first. An <input> with role="combobox" is a filterable combobox: type the
value to filter the list, then click the matching option. Long option lists
(countries, states) are paginated, so the option you want is not present until you
filter -- clicking blind is how a Country field ends up set to "Sierra Leone".
Always read the value back after setting it; an action that reports success has
often left the field wrong. Read back just that field rather than re-reading the
whole page -- everything you read stays in context and is re-processed on every
later step, so re-reading a large form after each field is the main thing that
makes a run expensive. Keep working a field until it holds the right value. Answering one question can also reveal a new required
field, so re-check the form before submitting.

Applications are often multi-page: an upload/parse step, then the real form, then review.
Work through every page until the application is actually submitted. ATS systems pre-fill
fields by parsing the resume and frequently get them wrong -- check every pre-filled value
against the applicant profile above and correct it. Answer all required questions.
Skip honeypot fields that are hidden or say to leave them blank.
For phone fields that already show a country prefix, enter only the digits {phone_digits}.
Match any format hint shown in a field's placeholder text.

{submit_instruction}

== REPORTING THE OUTCOME ==
When you are done -- successfully or not -- report the result using the required schema.
Use `applied` only if the application was genuinely submitted and confirmed.
If the posting is closed or no longer accepting applications, use `expired`.
If an unsolvable CAPTCHA blocks you, use `captcha`.
Otherwise use `failed` with the most specific reason code that fits."""
