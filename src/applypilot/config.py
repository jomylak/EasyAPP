"""ApplyPilot configuration: paths, platform detection, user data."""

import logging
import os
import platform
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))

# Core paths
DB_PATH = APP_DIR / "applypilot.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"
SETTINGS_PATH = APP_DIR / "settings.json"
# Live state of an in-flight apply run, written by apply/dashboard.py and read
# by the web server. A file rather than a DB table on purpose: worker progress
# updates land once or twice a second per worker, and putting that much write
# traffic through the same WAL the workers commit their real outcomes to buys
# contention for no durability -- mark_result() is already the durable record.
RUN_STATE_PATH = APP_DIR / "run_state.json"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Separate from CHROME_WORKER_DIR on purpose: apply workers get reset/cloned
# between runs (see chrome.setup_worker_profile), which would silently wipe a
# signed-in Jobright session. This profile is enrichment-only and never reset,
# so logging into Jobright here once keeps working across every future run.
ENRICHMENT_PROFILE_DIR = APP_DIR / "chrome-enrichment-profile"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def playwright_output_dir() -> str:
    """Where Playwright MCP spills page snapshots and console logs.

    Shared by both apply backends and every worker. One directory for all of
    them is fine: filenames are timestamped, and the size-capped eviction can
    only ever discard debug spill that nothing reads back.
    """
    d = APP_DIR / "playwright-output"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def load_profile() -> dict:
    """Load user profile from ~/.applypilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.applypilot/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_terminal_company_policy() -> dict:
    """Load researched/verified per-company post-grad-internship policy.

    Populated by one-time external research (official FAQs, anecdotal
    reports), not by the scorer -- see compute_company_pattern_terminal()
    in scoring/scorer.py for how a company marked accepts_post_grad: true
    here promotes that company's is_terminal_internship_likely rows.
    """
    import yaml
    path = CONFIG_DIR / "terminal_company_policy.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def _quirks_path(ats: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", ats.lower()).strip("_")
    return CONFIG_DIR / "known_quirks" / f"{slug}.md"


def load_known_quirks(ats: str | None) -> str:
    """Load verified widget-handling fallbacks for one ATS platform.

    Keyed by platform (Workday, SAP SuccessFactors, ...), never by employer --
    the same widget bug recurs across every tenant on a platform, but the
    literal DOM structure does not, so caching structure instead of behavior
    would misfire on the next company's instance of the same ATS.
    """
    if not ats:
        return ""
    path = _quirks_path(ats)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def append_known_quirk(ats: str | None, entry: str) -> None:
    """Append one verified fallback fix to an ATS's quirks file.

    Called only after a run that actually completed, and only with a fix the
    agent confirmed worked -- an unverified "fix" is worse than no cache entry
    at all, since future runs would trust it blindly.
    """
    entry = (entry or "").strip()
    if not ats or not entry:
        return
    path = _quirks_path(ats)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if entry in existing:
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + f"- {entry}\n")


def _issues_path(ats: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", ats.lower()).strip("_")
    return CONFIG_DIR / "known_issues" / f"{slug}.md"


def load_known_issues(ats: str | None) -> str:
    """Load recorded failure modes for one ATS platform.

    Distinct from known_quirks: a quirk is a *verified fix*, safe to apply;
    an issue is just "a past run got stuck here" -- a heads-up, not something
    to act on blindly. Keyed by platform for the same reason quirks are: the
    same CAPTCHA vendor or widget bug recurs across every tenant on a
    platform, but the literal DOM structure does not.
    """
    if not ats:
        return ""
    path = _issues_path(ats)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def append_known_issue(ats: str | None, entry: str) -> None:
    """Append one failure-mode note to an ATS's issues file.

    Called after a run that did NOT reach RESULT:APPLIED, with whatever the
    agent reported blocked it. Unlike append_known_quirk this has no
    trusted-writer restriction -- a wrong "watch out for X" only wastes a
    little of the next run's attention, whereas a wrong "verified fix" makes
    it click the wrong thing, so the bar for recording it is much lower.
    """
    entry = (entry or "").strip()
    if not ats or not entry:
        return
    path = _issues_path(ats)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if entry in existing:
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + f"- {entry}\n")


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    # Queue ordering only -- never rewrites the stored fit_score. A job's
    # real chance of still being open falls off the longer it's sat in the
    # queue, but the LLM's skill-match judgment doesn't change, so age is
    # applied as a priority penalty at pick-time instead of corrupting the
    # score itself. 0.05/day means a 20-day-old job loses about 1 point of
    # priority -- enough to let a fresher, slightly-lower-scored job jump
    # ahead of a stale one, not enough to bury an otherwise excellent match.
    "job_age_decay_per_day": 0.15,
    # How far back discovery accepts a posting's date. 21 (3 weeks, per
    # explicit user preference) supports an occasional big catch-up sweep;
    # run discovery more often (daily/hourly) with this same window and
    # near-duplicate postings are simply skipped by the url PRIMARY KEY, so
    # a wide window is safe to leave on permanently rather than needing a
    # separate "sweep vs sync" mode. APPLYPILOT_DISCOVERY_DAYS overrides
    # this for a single batch (e.g. a narrower one-week sweep) without
    # touching the permanent default.
    "discovery_posted_within_days": int(os.environ.get("APPLYPILOT_DISCOVERY_DAYS", 21)),
    # Extra pause after each Jobright Apply-flow click-through during
    # enrichment (see resolve_original_job_url). Unverified fix for a real
    # rate-limiting problem observed at batch volume -- adjust based on
    # what the moderate-batch test actually shows, this is a starting guess.
    "jobright_resolve_pacing_seconds": 8.0,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
    # Playwright MCP dumps a page snapshot + console log per browser step.
    # Left at its default it writes ".playwright-mcp/" into whatever the
    # cwd is -- 412 files / 7.4 MB accumulated in the repo root. Nothing
    # ever reads these back; they are debug spill, so they go under the
    # app dir with a size cap that evicts the oldest.
    "playwright_output_max_bytes": 200_000_000,
    # --- Goose backend (the default) ---
    # Best measured cost/reliability tradeoff on real ATS forms: $0.045 and
    # ~140 turns on a Workday application, ~99% cache-hit ratio. GLM 5.3 Flash
    # and DeepSeek V4 Flash Vision Exp both came in at $0.33-0.37 on the same
    # work -- GLM read Playwright's snapshot files through the shell instead of
    # using browser_find (now guarded against in the prompt), DeepSeek leaned
    # heavily on browser_evaluate. Override with `goose_model` in settings.json.
    "goose_model": "xiaomi/mimo-v2.5",
    "goose_provider": "openrouter",
    # Passed to Goose as GOOSE_THINKING_EFFORT, which Goose forwards to
    # OpenRouter as reasoning:{"effort": ...} -- keeps reasoning ON (unlike
    # llm.py's reasoning:{"enabled": False} for the scoring model) but caps
    # how many thinking tokens MiMo spends per turn. "low" was measured to
    # still set reasoning:true while trimming the thinking-token volume that
    # was the main driver of the ~15s-per-action latency. Override with
    # `goose_thinking_effort` in settings.json (off | low | medium | high | max).
    "goose_thinking_effort": "low",
    # Healthy runs top out around 140 turns; this leaves headroom for a long
    # multi-page form without letting a lost model loop forever.
    "goose_max_turns": 300,
    # Same tool, same arguments, this many times in a row means it is stuck.
    "goose_max_tool_repetitions": 15,
    # Wall-clock cap on one Goose run. --max-turns bounds turns, not time, and
    # a cheap model can sit in a slow tool call well past the point of use.
    # ~20s per action on a cheap model, with 45+ actions in a real form.
    "goose_timeout": 2400,
}


DEFAULT_SETTINGS: dict = {
    # Which engine drives the browser during apply: "goose" (the Goose CLI on
    # a cheap OpenRouter model -- the verified default) or "claude" (Claude
    # Code CLI, costs subscription quota). Override per run with
    # `applypilot apply --backend`.
    "apply_backend": "goose",
    # Fallback price per application, in USD, used only until enough real runs
    # have accumulated to measure it (see costs.estimate_batch). Seeded from
    # measured numbers: Goose on MiMo-V2.5 came in at ~$0.04 on a Workday
    # application, and Claude has averaged around $1.16 across its runs.
    # Anything here is a guess by definition -- the UI is expected to say so,
    # and costs.estimate_batch stops consulting these the moment there are
    # enough real runs to take a median from.
    "cost_defaults": {"goose": 0.04, "claude": 1.20},
    # Backend to retry a job on when the primary one fails for a reason that
    # looks like the *engine* gave up rather than the job being genuinely
    # inapplicable (see outcomes.FALLBACK_REASONS). Claude is the stronger,
    # more expensive driver, so it is worth one attempt on the jobs Goose
    # could not finish -- but only those. Set to null to disable the retry.
    "apply_fallback_backend": "claude",
    # Soft daily stops, checked once per acquire_job() call (launcher.py).
    # None means no cap. Hitting either just stops new jobs from being
    # claimed for the rest of the day -- a job already in_progress finishes
    # rather than being killed mid-application, since the money to get it
    # that far is already spent. Read from today's slice of the jobs table
    # itself (apply_cost_usd, apply_status), not a separate counter.
    "max_daily_spend_usd": None,
    "max_daily_applications": None,
    # Per-job ceilings, overridable here; fall back to config.DEFAULTS when
    # unset. max_apply_attempts also gates the ranked queue (see
    # launcher._select_ranked); apply_timeout is the Claude backend's
    # wall-clock cap, goose_timeout the Goose backend's.
    "max_apply_attempts": None,
    "apply_timeout": None,
    "goose_timeout": None,
    "goose_max_turns": None,
    "goose_max_tool_repetitions": None,
    # Whether a Goose run may write to the per-ATS known-quirks cache when it
    # reports RESULT:APPLIED with a QUIRK line.
    #
    # Reading the cache is always on for every backend. Writing is a setting
    # because the cache is shared: a quirk Goose records is later fed to
    # Claude runs too. The Claude backend deliberately restricts writes to
    # sonnet/opus after Haiku was seen fabricating RESULT:APPLIED with blank
    # fields, and a cheap model self-reporting a fix carries the same risk of
    # poisoning the cache for every future run on that platform. Turn this off
    # if bad quirk entries start showing up.
    "goose_writes_quirks": True,
    # When False, the tailor stage skips the LLM entirely and just uses the
    # base resume for every application (see scoring/tailor.py). Flip this
    # back to True once LaTeX tailoring is wired up.
    "tailoring_enabled": False,
    "cover_letters_enabled": False,
    # Per-install default for the Browse tab's "Eligible for me" filter
    # (confirmed-terminal OR likely-terminal internships only). Not every
    # user of this codebase is graduating soon -- a candidate who genuinely
    # can only apply to terminal/likely-terminal internships wants this on
    # by default every time they open the dashboard; a continuing student
    # wants to see the full internship list by default. Purely a starting
    # value for GlobalFilters.eligible_only on page load -- the pill in the
    # UI still toggles it per-session same as any other filter, this just
    # decides what state it starts in for THIS install (settings.json is
    # per-~/.applypilot, so two people running their own installs set this
    # independently).
    "default_eligible_internships_only": False,
    # Apply-queue ordering blends two independent judgements: fit_score (how
    # well the resume matches the JD) and desirability_score (how much the
    # candidate actually wants the job -- prestige, pay, location). They're
    # kept as separate numbers so neither quietly absorbs the other, and
    # weighted here rather than in code so ordering can be re-tuned without
    # re-scoring: only compute_desirability() re-runs, and it makes no LLM
    # calls. An even split is a starting point to adjust once the resulting
    # order is visible, not a considered final value.
    # 70/30, not an even split: desirability has to inform the order without
    # overturning it. At 50/50 a fit-5 firmware role at a big name outranked
    # ~33 better-matched jobs, which defeats the point of the prestige
    # override in prestige_override_tiers below -- those postings are meant to
    # be applied to *after* the genuine matches, not ahead of them.
    "fit_weight": 0.7,
    "desirability_weight": 0.3,
    # Weights within desirability itself. A component with no signal (an
    # unknown company, a posting that states no pay) is dropped and the rest
    # renormalised, rather than counted as a zero -- most postings state no
    # pay, so treating absence as bad would rank the minority that disclose
    # above everything else on that basis alone.
    "prestige_weight": 0.4,
    "location_weight": 0.4,
    "pay_weight": 0.2,
    # Ranked above the other PREFERRED_METROS (scoring/scorer.py): the
    # candidate already lives in the NYC area, so a job here means no
    # relocation at all, not merely a preferred city.
    "preferred_city": "New York",
    # [min_prestige, min_fit] pairs letting a strong enough employer into the
    # queue below the normal fit bar: one application's cost against an
    # asymmetric upside. Applied by fit_gate_sql() to both the tailor stage
    # and the apply queue. Set to [] to require fit alone.
    #
    # These only work because the eligibility gate independently rejects roles
    # the candidate genuinely cannot fill (degree level, clearance, non-US,
    # 3+ years of professional experience) -- without that, a lower fit bar at
    # a prestigious company would mostly buy applications to senior roles.
    # Worth revisiting if the applications these admit turn out to fail or go
    # unanswered at a noticeably higher rate than the fit>=7 ones; find them
    # with: SELECT * FROM jobs WHERE applied_at IS NOT NULL AND fit_score < 7.
    "prestige_override_tiers": [[8, 6], [9, 5], [10, 4]],
    # Bar for is_terminal_internship, the one flag acquire_job() sorts on
    # ahead of any composite score. Because that override is absolute, both
    # bars matter: the fit bar keeps out weak matches, and the desirability
    # bar keeps out jobs not worth jumping the queue for. Set the
    # desirability bar to 0 to rank purely on fit and stated evidence.
    # Flat desirability bump for an internship that offers housing or
    # relocation support. Applied outside the weighted components so it
    # reaches all ~250 postings that offer it, rather than only the quarter
    # that happen to sit near a pay-tier boundary. Not scaled by the stipend
    # size: just 3 of 1090 internships state a figure.
    "housing_bonus": 0.5,
    # Same flat-bump treatment as housing_bonus: a remote internship is worth
    # more regardless of where it's based, so this applies on top of
    # _location_desirability's own "Remote" tier (7.0) rather than being the
    # only thing that recognizes remote at all.
    "remote_bonus": 0.5,

    # Bars for is_terminal_internship / is_terminal_internship_likely. Gated
    # on fit_score + company_prestige directly (not desirability_score) --
    # desirability folds in location/pay too, which muddies "is this company
    # actually reputable" with "is this a good deal for me," and the
    # candidate wants those judged separately here. Below-floor pay is still
    # excluded unconditionally by pay_below_floor regardless of this bar.
    "terminal_min_fit": 6,
    "terminal_min_prestige": 6,

    # Bars for is_remote_spring_internship. Looser than the terminal bars --
    # this route to the priority tier needs no grad-date evidence at all, so
    # the risk of prioritizing a merely-okay match is just "worth an
    # application," not "silently repeats a mismatch." Still gated (not 0)
    # to keep out genuine bottom-tier noise.
    "remote_spring_min_fit": 5,
    "remote_spring_min_prestige": 5,

    # The candidate has exactly one graduation date printed on every resume.
    # There is deliberately no second "returning student" identity to fall
    # back on -- that idea was scrapped, so a posting that demands a later
    # graduation date is simply not applicable.
    #
    # start_date is configured separately rather than computed from
    # graduation_date: the gap between graduating and being available to
    # start is not a fixed offset, so any single formula would be wrong for
    # some terms.
    "graduation_date": "May 2027",
    "earliest_start_date": "August 2027",
    # When the candidate actually started their degree program -- distinct
    # from graduation_date/earliest_start_date above. Some ATS education
    # sections ask for this explicitly; without a configured value the
    # applying agent has to guess it off the resume (which only prints the
    # expected graduation date), and has guessed wrong before.
    "education_start_date": "August 2023",

    # Which resume file to attach, by scoring.router.route_resume_track's
    # output (swe | aiml | data) -- three hand-authored LaTeX resumes, one
    # grad date shared by all of them (see graduation_date above; the
    # grad-year axis of the old resume_variants system is gone, only the
    # track axis survived). get_resume_paths falls back to "default" when a
    # track's files aren't on disk yet, so this keeps working before all
    # three PDFs are exported from Overleaf.
    "default_resume_track": "swe",
    # Filenames chosen deliberately plain -- browser_file_upload attaches
    # the file by its literal path with no rename step, so whatever these
    # are named is exactly what an ATS/reviewer sees as the attachment.
    # "resume_swe.pdf" next to "resume_aiml.pdf"/"resume_data.pdf" reads as
    # auto-generated; a name/year+number scheme doesn't.
    "resume_tracks": {
        "swe": {"pdf": "Jakub_Omylak_2026_Resume1.pdf", "txt": "Jakub_Omylak_2026_Resume1.txt"},
        "aiml": {"pdf": "Jakub_Omylak_2026_Resume2.pdf", "txt": "Jakub_Omylak_2026_Resume2.txt"},
        "data": {"pdf": "Jakub_Omylak_2026_Resume3.pdf", "txt": "Jakub_Omylak_2026_Resume3.txt"},
        # Legacy, pre-track. Fallback target only -- never routed to.
        "default": {"pdf": "resume.pdf", "txt": "resume.txt"},
    },
}



# ── Company tiers ─────────────────────────────────────────────────────────
#
# The candidate's hard requirement is that every posting from a household-name
# tech company gets applied to, regardless of how the skills-fit score reads.
# Fit is near-useless as a discriminator here (most postings match most of
# their skills) and it was actively burying these: Meta internships average
# fit 3.4, Anthropic new-grad 2.0, Apple new-grad 4.0 -- all at prestige 10,
# and none had ever been applied to. So tier membership is a hard pin ABOVE
# the ranking, not another weighted term that a low fit could outvote.
#
# Matched case-insensitively against the `company` column, on a normalized
# form (punctuation and legal suffixes stripped) so "Meta Platforms, Inc."
# and "meta" both land.
TIER1_COMPANIES = [
    "Google", "Alphabet", "Meta", "Facebook", "Amazon", "AWS", "Apple",
    "Netflix", "Microsoft", "NVIDIA", "OpenAI", "Anthropic", "Tesla",
    "LinkedIn", "TikTok", "ByteDance",
]

TIER1_ADJACENT = [
    "Stripe", "Databricks", "Uber", "Lyft", "Airbnb", "Salesforce", "Oracle",
    "Adobe", "Snowflake", "Palantir", "Datadog", "Coinbase", "DoorDash",
    "Instacart", "Pinterest", "Snap", "Roblox", "Figma", "Notion", "Ramp",
    "Plaid", "Bloomberg", "Citadel", "Citadel Securities", "Jane Street",
    "Two Sigma", "Jump Trading", "Hudson River Trading", "D. E. Shaw",
    "Optiver", "IMC Trading", "Goldman Sachs", "JPMorgan", "JPMorgan Chase",
    "Morgan Stanley", "IBM", "Intel", "AMD", "Qualcomm", "Cisco", "VMware",
    "Dropbox", "Block", "Square", "Twilio", "Cloudflare", "MongoDB",
    "HashiCorp", "Atlassian", "Waymo", "Cruise", "SpaceX", "Rivian",
    "Scale AI", "Anduril", "Reddit", "Discord", "Spotify", "Shopify",
    "PayPal", "eBay", "Expedia", "Zillow", "Yelp", "Robinhood", "Affirm",
]

# Any company the scorer rated this reputable counts as tier-adjacent even if
# it isn't named above -- the list is a floor, not a ceiling, so a company
# nobody thought to add still surfaces.
TIER_PRESTIGE_FLOOR = 9


def load_settings() -> dict:
    """Load ~/.applypilot/settings.json, falling back to defaults for missing keys.

    Unlike searches.yaml, this file has no package-shipped example -- it's
    created with sane defaults on first read if it doesn't exist yet.
    """
    import json

    if not SETTINGS_PATH.exists():
        return json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy

    try:
        user_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_SETTINGS))

    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merged.update({k: v for k, v in user_settings.items() if k != "resume_tracks"})
    if "resume_tracks" in user_settings:
        merged["resume_tracks"].update(user_settings["resume_tracks"])
    return merged


def save_settings(settings: dict) -> None:
    """Write settings back to ~/.applypilot/settings.json."""
    import json
    ensure_dirs()
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def get_resume_paths(track: str | None = None) -> tuple[Path, Path]:
    """Resolve (txt_path, pdf_path) for a resume track (swe | aiml | data).

    No track (or an unrecognized one) resolves to the plain resume.txt /
    resume.pdf. A recognized track whose files aren't on disk yet falls back
    to the same plain resume rather than erroring, so the pipeline keeps
    running before all three PDFs are exported from Overleaf.
    """
    if not track:
        return RESUME_PATH, RESUME_PDF_PATH
    settings = load_settings()
    tracks = settings.get("resume_tracks", {})
    cfg = tracks.get(track)
    if not cfg:
        return RESUME_PATH, RESUME_PDF_PATH
    txt_path, pdf_path = APP_DIR / cfg["txt"], APP_DIR / cfg["pdf"]
    if txt_path.exists() and pdf_path.exists():
        return txt_path, pdf_path
    log.warning("Resume track %r has no files on disk; using the plain resume instead.", track)
    return RESUME_PATH, RESUME_PDF_PATH


def get_grad_and_start_dates() -> tuple[str, str]:
    """The candidate's single true graduation date and earliest start date."""
    settings = load_settings()
    return (settings.get("graduation_date", "May 2027"),
            settings.get("earliest_start_date", "August 2027"))



def get_tier_companies() -> tuple[list[str], list[str]]:
    """(tier1, adjacent) company names, with settings.json overriding the
    built-in lists when it carries `tier1_companies` / `tier1_adjacent`."""
    settings = load_settings()
    return (list(settings.get("tier1_companies") or TIER1_COMPANIES),
            list(settings.get("tier1_adjacent") or TIER1_ADJACENT))


def normalize_company(name: str | None) -> str:
    """Lowercase a company name and strip punctuation and legal suffixes, so
    "Meta Platforms, Inc." and "meta" compare equal."""
    import re as _re
    blob = _re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    blob = _re.sub(
        r"\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|"
        r"technologies|technology|labs|platforms|securities|systems)\b",
        " ", blob)
    return " ".join(blob.split())


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


def get_apply_proxy(session_id: str) -> dict | None:
    """Parse APPLY_PROXY into one sticky-session proxy for a single job.

    Format: host:port:user:pass, same as the scraping-only PROXY var (see
    discovery/jobspy.py's parse_proxy) but kept separate since this is a
    different concern -- apply-time browsing + CAPTCHA solving, not discovery.
    A literal "{session}" in the user field is substituted with session_id,
    so a residential-proxy provider's sticky-session syntax (each provider
    spells it differently, e.g. appending "-session-<id>" to the username)
    can be configured without hardcoding any one vendor's format. Chrome and
    CapSolver MUST share the same session_id for a given job -- that's the
    whole point of routing both through the same egress IP.

    Returns None if APPLY_PROXY isn't set.
    """
    load_env()
    raw = os.environ.get("APPLY_PROXY", "").strip()
    if not raw:
        return None

    parts = raw.split(":")
    if len(parts) != 4:
        raise ValueError(
            f"APPLY_PROXY format not recognized: {raw!r}. "
            "Expected host:port:user:pass (user may contain '{session}')."
        )
    host, port, user, passwd = parts
    user = user.replace("{session}", session_id)
    # CapSolver's "proxy" task field wants "type:ip:port:user:pass" -- verify
    # this against CapSolver's current docs before relying on it; "http" is
    # the common case but some providers require "socks5".
    proxy_type = os.environ.get("APPLY_PROXY_TYPE", "http").strip() or "http"
    return {
        "host": host,
        "port": port,
        "user": user,
        "pass": passwd,
        "capsolver": f"{proxy_type}:{host}:{port}:{user}:{passwd}",
    }


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Chrome + an apply backend that can run

    Either backend unlocks tier 3 -- Goose (with an OpenRouter key) or the
    Claude Code CLI. Requiring Claude specifically would report tier 2 for a
    perfectly working Goose-only setup, which is the default install.
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_goose = (shutil.which("goose") is not None
                 and bool(os.environ.get("OPENROUTER_API_KEY", "").strip()))
    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if (has_goose or has_claude) and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
        missing.append("LLM API key — run [bold]applypilot init[/bold] or set GEMINI_API_KEY")
    if required >= 3:
        has_goose = (shutil.which("goose") is not None
                     and bool(os.environ.get("OPENROUTER_API_KEY", "").strip()))
        if not has_goose and not shutil.which("claude"):
            missing.append(
                "An apply backend — either Goose (install from "
                "[bold]https://block.github.io/goose[/bold] and set OPENROUTER_API_KEY) "
                "or the Claude Code CLI ([bold]https://claude.ai/code[/bold])"
            )
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
