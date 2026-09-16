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

_RESULT_RE = re.compile(r"RESULT:(\d+)\|([a-z_]+)\|(.*)")


def parse_scan_results(output: str, job_count: int) -> list[tuple[int, str, str]]:
    """Pull ``(1-based index, status, evidence)`` triples out of a goose
    transcript. Ignores any line whose status isn't in ``STATUSES`` or whose
    index falls outside ``1..job_count`` -- goose output is free text, not
    trusted structured output (a model can echo an example back verbatim).
    """
    out = []
    for match in _RESULT_RE.finditer(output):
        idx, status, evidence = int(match.group(1)), match.group(2), match.group(3).strip()
        if status in STATUSES and 1 <= idx <= job_count:
            out.append((idx, status, evidence[:200]))
    return out
