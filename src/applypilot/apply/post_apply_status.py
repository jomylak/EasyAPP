"""Post-apply outcome vocabulary -- what happened after the application went in.

Separate from ``outcomes.py``, which describes how the *apply attempt itself*
went (submitted vs. failed vs. needs review). This is the next stage: what the
employer said back, read out of the candidate's inbox by
``scripts/scan_gmail_status.py`` or set by hand from the dashboard.
"""

import re

# Ordered roughly by how far the application has progressed, so the dashboard
# can pick the "furthest" status when more than one signal exists for a job.
STATUSES: tuple[str, ...] = ("none", "oa", "interview", "rejected", "offer")

STATUS_LABELS: dict[str, str] = {
    "none": "No response",
    "oa": "OA",
    "interview": "Interview",
    "rejected": "Rejected",
    "offer": "Offer",
}

_RESULT_RE = re.compile(r"RESULT:(\d+)\|([a-z_]+)\|([^|\n]*)\|([^\n]*)")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NEWJOB_RE = re.compile(r"NEWJOB:([^|\n]*)\|([^|\n]*)\|([^|\n]*)\|([^|\n]*)")


def parse_scan_results(output: str, job_count: int) -> list[tuple[int, str, str | None, str]]:
    """Pull ``(1-based index, status, event_date, evidence)`` quads out of a
    goose transcript. ``event_date`` is an OA's stated deadline or an
    interview's scheduled date, ISO ``YYYY-MM-DD``, or None when the email
    didn't state one. Ignores any line whose status isn't in ``STATUSES``,
    whose date fails the ISO check, or whose index falls outside
    ``1..job_count`` -- goose output is free text, not trusted structured
    output (a model can echo an example back verbatim).
    """
    out = []
    for match in _RESULT_RE.finditer(output):
        idx, status, event_date, evidence = (
            int(match.group(1)), match.group(2), match.group(3).strip(), match.group(4).strip(),
        )
        if status not in STATUSES or not (1 <= idx <= job_count):
            continue
        if not _DATE_RE.match(event_date):
            event_date = None
        out.append((idx, status, event_date, evidence[:200]))
    return out


def parse_new_job_lines(output: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """Pull ``(company, title, url, applied_date)`` quads out of a goose
    transcript's ``NEWJOB:`` lines -- application-confirmation emails found
    for a job the pipeline never applied to (the candidate applied by hand).
    Any field goose reported as 'unknown'/'none' comes back as None.
    """
    def _clean(v: str) -> str | None:
        v = v.strip()
        return None if not v or v.lower() in ("unknown", "none") else v

    out = []
    for match in _NEWJOB_RE.finditer(output):
        company = _clean(match.group(1))
        if not company:
            continue
        title = _clean(match.group(2))
        url = _clean(match.group(3))
        applied_date = _clean(match.group(4))
        if applied_date and not _DATE_RE.match(applied_date):
            applied_date = None
        out.append((company, title, url, applied_date))
    return out
