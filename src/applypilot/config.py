"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import re
import shutil
from pathlib import Path

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
    # How far back discovery accepts a posting's date. 14 supports an
    # occasional big catch-up sweep; run discovery more often (daily/hourly)
    # with this same window and near-duplicate postings are simply skipped
    # by the url PRIMARY KEY, so a wide window is safe to leave on
    # permanently rather than needing a separate "sweep vs sync" mode.
    "discovery_posted_within_days": 14,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
    # --- Skyvern backend ---
    # Skyvern's own MAX_STEPS_PER_RUN defaults to 10, which terminates a
    # multi-page ATS form partway through; we pass this per task instead.
    "skyvern_max_steps": 50,
    # Measured: ~20s per action on a free OpenRouter model, and a multi-page
    # ATS form runs to 45+ actions. 900s cut a healthy run off mid-form.
    "skyvern_timeout": 2400,
    "skyvern_base_url": "http://localhost:8000",
    # Loopback port serving the tailored resume to Skyvern (+ worker_id).
    "skyvern_file_port_base": 8100,
    # Loopback port hosting the email-verification (totp_url) endpoint.
    "skyvern_totp_port_base": 8200,
    # How long the totp endpoint waits for a verification email to arrive.
    "verification_wait_seconds": 45,
    # How often the background watcher looks for one-time login links.
    "verification_link_poll_seconds": 10,
}


DEFAULT_SETTINGS: dict = {
    # Which engine drives the browser during apply: "claude" (Claude Code CLI,
    # costs subscription quota) or "skyvern" (local Skyvern server on a cheap
    # model). Override per run with `applypilot apply --backend`.
    "apply_backend": "claude",
    # When False, the tailor stage skips the LLM entirely and just uses the
    # base resume for every application (see scoring/tailor.py). Flip this
    # back to True once LaTeX tailoring is wired up.
    "tailoring_enabled": False,
    "cover_letters_enabled": False,
    "default_resume_variant": "default",
    "resume_variants": {
        "default": {
            "pdf": "resume.pdf",
            "txt": "resume.txt",
            "grad_date": "May 2027",
        },
        "returning_2028": {
            "pdf": "resume_2028.pdf",
            "txt": "resume_2028.txt",
            "grad_date": "May 2028",
        },
    },
}


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
    merged.update({k: v for k, v in user_settings.items() if k != "resume_variants"})
    if "resume_variants" in user_settings:
        merged["resume_variants"].update(user_settings["resume_variants"])
    return merged


def save_settings(settings: dict) -> None:
    """Write settings back to ~/.applypilot/settings.json."""
    import json
    ensure_dirs()
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def get_resume_variant_paths(variant: str) -> tuple[Path, Path, str]:
    """Resolve (txt_path, pdf_path, grad_date) for a resume variant name.

    Falls back to the default variant if the requested one isn't configured.
    """
    settings = load_settings()
    variants = settings.get("resume_variants", {})
    cfg = variants.get(variant) or variants.get(settings.get("default_resume_variant", "default"))
    txt_path = APP_DIR / cfg["txt"]
    pdf_path = APP_DIR / cfg["pdf"]
    return txt_path, pdf_path, cfg.get("grad_date", "")


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


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
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
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
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
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
