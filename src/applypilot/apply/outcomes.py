"""Application outcome vocabulary, shared by every apply backend.

These reason codes are the contract between whatever drives the browser and
the database: they decide whether a job is retried, permanently abandoned, or
flagged for a human to look at. They previously lived in ``launcher``; they
moved here so backends can build on them without importing the launcher (which
imports the backends in turn).

The Skyvern backend also feeds these codes to Skyvern as its
``error_code_mapping``, so the model reports outcomes in the same vocabulary
the Claude Code path prints as ``RESULT:FAILED:<reason>``.
"""

# Reasons that mean "never try this job again".
PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

# Clean, understood reasons this job will never be applicable -- no human
# action needed, just a disqualification worth knowing about.
DISQUALIFIED_REASONS: set[str] = {
    "not_eligible_location", "not_eligible_salary", "already_applied",
    "expired", "captcha", "account_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

# Reasons where something unusual happened -- worth a human glance rather
# than a clean pass/fail (agent got stuck, hit a wall it didn't expect, or
# the resume it needed doesn't exist yet).
NEEDS_REVIEW_REASONS: set[str] = {
    "stuck", "no_result_line", "unknown", "page_error",
    "not_a_job_application", "sso_required", "login_issue",
    "unsafe_permissions", "unsafe_verification", "timeout",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")

# Statuses the worker loop treats as terminal outcomes in their own right,
# rather than as a `failed:<reason>` string.
PROMOTE_TO_STATUS: set[str] = {"captcha", "expired", "login_issue"}


def classify_review_status(reason: str) -> str | None:
    """Bucket a failure reason into 'disqualified', 'needs_review', or None (retry as usual)."""
    reason = (reason or "").lower()
    if reason in DISQUALIFIED_REASONS:
        return "disqualified"
    if reason in NEEDS_REVIEW_REASONS:
        return "needs_review"
    return None


def is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# Human-readable guidance for each reason code, handed to Skyvern as its
# error_code_mapping so the model knows which code fits which wall it hit.
ERROR_CODE_MAPPING: dict[str, str] = {
    "expired": "The posting is closed, filled, or no longer accepting applications.",
    "captcha": "A CAPTCHA blocks progress and cannot be solved.",
    "login_issue": "Could not sign in or create an account on the employer's own system.",
    "sso_required": "The site requires signing in through Google, Microsoft, or another SSO/OAuth provider.",
    "account_required": "An pre-existing account is required that this candidate does not have.",
    "already_applied": "The candidate has already applied to this posting.",
    "not_eligible_location": "The role is onsite or hybrid outside the acceptable area with no remote option.",
    "not_eligible_salary": "The compensation is below the candidate's stated floor.",
    "not_a_job_application": "This is not a job application -- it is a profile builder, talent network, freelancing marketplace, or assessment platform.",
    "unsafe_permissions": "The site demanded camera, microphone, screen sharing, or location access.",
    "unsafe_verification": "The site demanded video/audio verification, a selfie, an ID photo, or biometrics.",
    "site_blocked": "The site blocked automated access.",
    "cloudflare_blocked": "Cloudflare blocked access to the site.",
    "page_error": "The page is broken, blank, or returned a server error.",
    "stuck": "Made no progress after repeated attempts on the same page.",
}
