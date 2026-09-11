"""Canonical buckets for `jobs.apply_error`, for reporting.

`apply_error` itself stays free text (see outcomes.py's RESULT:FAILED:<reason>
contract) -- useful as the raw diagnostic on one job's detail view, but not
something a dashboard can GROUP BY directly: the agent appends a one-line note
after the reason code, and different sites/backends have historically spelled
the same underlying failure differently (e.g. an Indeed-specific block string
vs. a generic "site_blocked" -- both mean the same thing and must count
together). `normalize_failure_reason` maps any raw string down to one of a
small, stable set of categories, built on the vocabulary outcomes.py already
established, so the failure-reasons chart doesn't fragment into one bar per
one-off phrasing.
"""

import sqlite3

from applypilot.apply.outcomes import FALLBACK_REASONS, PERMANENT_FAILURES

# Ordered (category, display label, matcher) rules, first match wins. Reasons
# not covered by any rule below fall into "other" rather than minting a new
# singleton bucket -- see backfill_failure_categories for how "other" gets
# reviewed and promoted into a real rule over time.
# Substrings, not just prefixes -- the agent free-texts a reason for anything
# outside the RESULT_CODES list, and "this site detected the automation and
# blocked it" gets spelled a dozen different ways in practice ("indeed-
# blocks_automation", "site_blocks_automation", "bot_detected", "automation
# blocked", ...). Anything containing one of these substrings is the same
# underlying event -- the site's bot defenses stopped the run -- and must
# collapse to one bar, not fragment into a bar per phrasing.
_SITE_BLOCK_PREFIXES = ("site_blocked", "cloudflare", "blocked_by")
_SITE_BLOCK_SUBSTRINGS = ("blocks_automation", "block_automation", "bot_detect", "bot_block", "automation_blocked")

_REMAINING_PERMANENT = sorted(
    PERMANENT_FAILURES - set(_SITE_BLOCK_PREFIXES) - {"grad_date_mismatch"}
)
_REMAINING_FALLBACK = sorted(FALLBACK_REASONS)

FAILURE_LABELS: dict[str, str] = {
    "site_blocked": "Blocked by site",
    "grad_date_mismatch": "Graduation date mismatch",
    "duplicate": "Duplicate posting",
    "expired": "Posting expired",
    "captcha": "Captcha",
    "login_issue": "Login required",
    "not_eligible_location": "Not eligible (location)",
    "not_eligible_salary": "Not eligible (salary)",
    "already_applied": "Already applied",
    "account_required": "Account required",
    "not_a_job_application": "Not a job application",
    "unsafe_permissions": "Unsafe permissions requested",
    "unsafe_verification": "Unsafe verification requested",
    "sso_required": "SSO required",
    "dob_required": "Date of birth required",
    "stuck": "Agent got stuck",
    "no_result_line": "No result reported",
    "unknown": "Unknown",
    "page_error": "Page error",
    "timeout": "Timed out",
    "other": "Other",
}


def normalize_failure_reason(raw: str | None) -> str:
    """Map a raw `apply_error` string to one of FAILURE_LABELS' keys."""
    reason = (raw or "").strip().lower()
    if not reason:
        return "other"

    if reason.startswith("duplicate_of:"):
        return "duplicate"
    if reason.startswith("grad_date_mismatch"):
        return "grad_date_mismatch"
    if any(reason.startswith(p) for p in _SITE_BLOCK_PREFIXES):
        return "site_blocked"
    if reason == "blocked_by_cloudflare" or reason == "cloudflare_blocked":
        return "site_blocked"
    if any(s in reason for s in _SITE_BLOCK_SUBSTRINGS):
        return "site_blocked"

    for category in _REMAINING_PERMANENT:
        if reason == category or reason.startswith(category):
            return category
    for category in _REMAINING_FALLBACK:
        if reason == category:
            return category

    return "other"


def backfill_failure_categories(conn: sqlite3.Connection | None = None) -> int:
    """One-time pass: fill `apply_error_category` for existing rows.

    Idempotent -- only touches rows where the category is still NULL, so it's
    safe to re-run after new normalization rules are added (it just fills in
    whatever the old rules missed, without re-deriving rows already set).
    """
    from applypilot.database import get_connection

    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT url, apply_error FROM jobs "
        "WHERE apply_error IS NOT NULL AND apply_error_category IS NULL"
    ).fetchall()

    updates = [(normalize_failure_reason(r["apply_error"]), r["url"]) for r in rows]
    if updates:
        conn.executemany(
            "UPDATE jobs SET apply_error_category = ? WHERE url = ?", updates
        )
        conn.commit()
    return len(updates)
