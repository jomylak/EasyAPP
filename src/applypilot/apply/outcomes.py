"""Application outcome vocabulary, shared by every apply backend.

These reason codes are the contract between whatever drives the browser and
the database: they decide whether a job is retried, permanently abandoned, or
flagged for a human to look at. They previously lived in ``launcher``; they
moved here so backends can build on them without importing the launcher (which
imports the backends in turn).

Every backend prints them the same way -- ``RESULT:FAILED:<reason>`` -- so the
Goose and Claude paths are interchangeable from the launcher's point of view.
"""

# Reasons that mean "never try this job again".
PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    # Only ever printed when the form/posting requires a graduation date the
    # attached resume doesn't have. Used to be retryable because the pipeline
    # could swap to a second resume printed with a later graduation date --
    # that identity was retired (single true grad date only), so there is no
    # honest resume left to retry with.
    "grad_date_mismatch",
    # Same shape as grad_date_mismatch: the form needs a field (date of
    # birth, for pre-employment verification) that isn't in the candidate
    # profile at all, so a retry hits the identical wall. Confirmed for real
    # against IBM's application form on 2026-09-10 -- the agent correctly
    # refused to fabricate a DOB rather than risk the offer-rescission
    # warning those forms carry.
    "dob_required",
}

# Clean, understood reasons this job will never be applicable -- no human
# action needed, just a disqualification worth knowing about.
DISQUALIFIED_REASONS: set[str] = {
    "not_eligible_location", "not_eligible_salary", "already_applied",
    "expired", "captcha", "account_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "grad_date_mismatch", "dob_required",
}

# Reasons where something unusual happened -- worth a human glance rather
# than a clean pass/fail (agent got stuck, hit a wall it didn't expect, or
# the resume it needed doesn't exist yet).
NEEDS_REVIEW_REASONS: set[str] = {
    "stuck", "no_result_line", "unknown", "page_error",
    "not_a_job_application", "sso_required", "login_issue",
    "unsafe_permissions", "unsafe_verification", "timeout",
}

PERMANENT_PREFIXES: tuple[str, ...] = (
    "site_blocked", "cloudflare", "blocked_by",
    # The prompt asks the agent to append a one-line note to this same
    # RESULT line (e.g. "grad_date_mismatch -- dropdown only offered 2028
    # and 2029, no 2027 option"), so an exact match against the bare code
    # would silently miss every real occurrence.
    "grad_date_mismatch",
)

# Statuses the worker loop treats as terminal outcomes in their own right,
# rather than as a `failed:<reason>` string.
PROMOTE_TO_STATUS: set[str] = {"captcha", "expired", "login_issue"}


def classify_review_status(reason: str) -> str | None:
    """Bucket a failure reason into 'disqualified', 'needs_review', or None (retry as usual)."""
    reason = (reason or "").lower()
    if reason in DISQUALIFIED_REASONS or any(reason.startswith(p) for p in PERMANENT_PREFIXES):
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


# Failures worth one retry on the fallback backend (settings.json:
# `apply_fallback_backend`). These all mean "the engine driving the browser
# gave up", not "this job cannot be applied to" -- a stronger model may well
# get through where a cheap one lost the thread.
#
# Deliberately excludes every PERMANENT_FAILURE, and also the walls that are
# about the site rather than the driver: sso_required, unsafe_permissions and
# unsafe_verification block any agent equally, so retrying just burns quota.
FALLBACK_REASONS: set[str] = {
    "stuck", "no_result_line", "unknown", "page_error", "timeout",
}


def should_fall_back(result: str) -> bool:
    """Whether a failed run should be retried on the fallback backend."""
    if not result.startswith("failed:"):
        return False
    reason = result.split(":", 1)[1].strip().lower()
    return reason in FALLBACK_REASONS and not is_permanent_failure(result)
