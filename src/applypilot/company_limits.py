"""Per-company application caps.

Some employers cap how many applications they'll actually consider from one
candidate in a given window (confirmed via careers-site FAQs, or just
observed pattern) -- applying past that spends an apply run on a job that was
never reachable. `KNOWN_LIMITS` holds the confirmed, period-based cases;
everything else gets `DEFAULT_LIMIT` as a lifetime total ("total" period,
never resets), since most employers never state a renewing allowance and a
default that quietly resets every month would let the blanket cap be applied
to indefinitely.

Matching reuses the whole-word-prefix rule scorer.compute_company_tiers()
already established for `company_tier` -- "TikTok (ByteDance)" and "tiktok"
have to land in the same bucket, or the cap silently never fires.
"""

import sqlite3
from datetime import datetime, timezone

from applypilot import config

DEFAULT_LIMIT = 6

# period is "total" (lifetime, never resets), "month" (calendar month), or
# "season" (recruiting season: winter Dec-Feb, spring Mar-May, summer
# Jun-Aug, fall Sep-Nov -- the same boundaries the `term` column already
# uses elsewhere in the pipeline). Only companies with a *confirmed*
# renewing allowance get "month"/"season" here -- the blanket default is
# "total" precisely because most employers never state one.
KNOWN_LIMITS: dict[str, dict] = {
    "google": {"limit": 3, "period": "month"},
    "tiktok": {"limit": 2, "period": "season"},
    "bytedance": {"limit": 2, "period": "season"},
}


def _matches(a: str, b: str) -> bool:
    """Whole-word prefix match between two already-normalized names."""
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def get_limit(company: str | None) -> tuple[int, str]:
    """(limit, period) for a company name: a known override, or the blanket
    default. settings.json can override either half (`company_application_limits`,
    `default_company_application_limit`), same pattern as tier1_companies."""
    settings = config.load_settings()
    default_limit = settings.get("default_company_application_limit") or DEFAULT_LIMIT
    overrides = settings.get("company_application_limits") or KNOWN_LIMITS
    name = config.normalize_company(company)
    if not name:
        return default_limit, "total"
    for key, cfg in overrides.items():
        if _matches(name, config.normalize_company(key)):
            return cfg["limit"], cfg.get("period", "total")
    return default_limit, "total"


def _period_start(period: str, now: datetime | None = None) -> str:
    """ISO cutoff for 'month'/'season'/'total', to compare against applied_at.

    'total' means lifetime -- no cutoff, i.e. every applied_at ever recorded
    counts, which this represents as the epoch so the same ">=" comparison
    the other two periods use still works unchanged.
    """
    now = now or datetime.now(timezone.utc)
    if period == "total":
        return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()
    if period == "season":
        month = now.month
        if month == 12 or month <= 2:
            year = now.year if month == 12 else now.year - 1
            start = datetime(year, 12, 1, tzinfo=timezone.utc)
        elif month <= 5:
            start = datetime(now.year, 3, 1, tzinfo=timezone.utc)
        elif month <= 8:
            start = datetime(now.year, 6, 1, tzinfo=timezone.utc)
        else:
            start = datetime(now.year, 9, 1, tzinfo=timezone.utc)
    else:
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return start.isoformat()


def status_for(conn: sqlite3.Connection, company: str | None) -> dict:
    """Cap status for one company: applied count this period vs. its limit."""
    limit, period = get_limit(company)
    name = config.normalize_company(company)
    applied = 0
    if name:
        cutoff = _period_start(period)
        rows = conn.execute(
            "SELECT company FROM jobs WHERE apply_status = 'applied' AND applied_at >= ?",
            (cutoff,),
        ).fetchall()
        applied = sum(1 for r in rows if _matches(config.normalize_company(r["company"]), name))
    return {
        "company": company,
        "limit": limit,
        "period": period,
        "applied": applied,
        "remaining": max(0, limit - applied),
        "at_cap": applied >= limit,
    }


def all_statuses(conn: sqlite3.Connection) -> list[dict]:
    """Cap status for every company with at least one applied row this run,
    for the dashboard's per-company breakdown. Companies furthest into their
    cap (fewest remaining) sort first -- that's the useful read at a glance."""
    rows = conn.execute(
        "SELECT DISTINCT company FROM jobs "
        "WHERE apply_status = 'applied' AND company IS NOT NULL AND company != ''"
    ).fetchall()
    seen: dict[str, dict] = {}
    for r in rows:
        name = config.normalize_company(r["company"])
        if not name or name in seen:
            continue
        seen[name] = status_for(conn, r["company"])
    return sorted(seen.values(), key=lambda s: (s["remaining"], -s["applied"]))
