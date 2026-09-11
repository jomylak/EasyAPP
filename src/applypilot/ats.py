"""Applicant Tracking System detection.

Which ATS a posting routes to determines how hard it is to fill. SAP
SuccessFactors, for example, uses a paginated combobox (``rcmpaginatedselect``,
committed through SAP's ``juic`` event bus) that is not an HTML ``<select>`` --
agents on both the accessibility-tree and vision paths failed to set it,
repeatedly landing on the wrong country.

Detection is pure pattern matching against the URL and page HTML: **no LLM call
and no network request**, so it adds nothing to the Gemini free-tier budget.
Job boards hide the real ATS behind redirects (jobright.ai, LinkedIn), so the
apply-time URL is usually a better signal than anything stored at discovery.
"""

import re

# Ordered most- to least-specific: some pages carry several vendors' scripts,
# and the first match wins.
_URL_PATTERNS: list[tuple[str, str]] = [
    # The last alternative is SuccessFactors' career-site URL shape on a vanity
    # domain: /job/<City>-<Title>-<ST>-<ZIP>/<numeric requisition id>/
    # e.g. careers.qorvo.com/job/Richardson-Full-Stack-TX-75080/1424716200/
    ("SAP SuccessFactors", r"successfactors\.|/careers/?\?company=|jobs\.sap\.com|career\d*\.sap|/job/[^/]+-[a-z]{2}-\d{5}/\d{6,}"),
    ("Workday",            r"myworkdayjobs\.com|workday\.com|wd\d+\.myworkday"),
    ("Greenhouse",         r"greenhouse\.io|boards\.greenhouse|job-boards\.greenhouse"),
    ("Lever",              r"jobs\.lever\.co|lever\.co/"),
    ("Ashby",              r"jobs\.ashbyhq\.com|ashbyhq\.com"),
    ("iCIMS",              r"icims\.com"),
    ("Taleo",              r"taleo\.net|tbe\.taleo"),
    ("Oracle HCM",         r"oraclecloud\.com|/hcmUI/|fa-[a-z]+-saasfaprod"),
    ("SmartRecruiters",    r"smartrecruiters\.com"),
    ("Jobvite",            r"jobvite\.com"),
    ("BrassRing",          r"brassring\.com|kenexa\."),
    ("Dayforce",           r"dayforcehcm\.com"),
    ("Phenom",             r"phenompeople\.com"),
    ("Eightfold",          r"eightfold\.ai"),
    ("Workable",           r"workable\.com"),
    ("BambooHR",           r"bamboohr\.com"),
    ("Paylocity",          r"paylocity\.com"),
    ("ADP",                r"adp\.com/.*careers|workforcenow\.adp"),
]

# DOM/script fingerprints, for when the URL is a vanity domain fronting an ATS.
_HTML_PATTERNS: list[tuple[str, str]] = [
    ("SAP SuccessFactors", r"rcmpaginatedselect|juic\.fire|sfcareer|/sf/careers"),
    ("Workday",            r"wd-[A-Za-z]+-[Ff]ieldSet|workdayjobs"),
    ("Greenhouse",         r"greenhouse_job_board|grnhse_app"),
    ("Lever",              r"lever-application|postings\.lever"),
    ("iCIMS",              r"icims_content|iCIMS_MainWrapper"),
    ("Taleo",              r"taleo|requisitionDescriptionInterface"),
    ("Oracle HCM",         r"oj-combobox|oracle-jet|/hcmUI/"),
]

# Redirect shims that never reveal the ATS -- knowing it's one of these tells
# us the real platform is only visible after following the link.
_AGGREGATORS = r"jobright\.ai|linkedin\.com/jobs|indeed\.com|glassdoor\.|intern-list\.com|newgrad-jobs\.com|ziprecruiter\."


def detect_ats(url: str | None = None, html: str | None = None) -> str | None:
    """Identify the ATS behind a posting.

    Args:
        url: Application URL. The final URL after redirects is far more
            informative than an aggregator link.
        html: Optional page source, for vanity domains that front an ATS.

    Returns:
        Platform name, "aggregator (unresolved)" when the URL is only a
        redirect shim, or None if nothing matched.
    """
    # NB: lowercased, so URL patterns above must not rely on capital letters.
    blob = (url or "").lower()
    for name, pattern in _URL_PATTERNS:
        if re.search(pattern, blob):
            return name

    if html:
        for name, pattern in _HTML_PATTERNS:
            if re.search(pattern, html, re.I):
                return name

    if blob and re.search(_AGGREGATORS, blob):
        return "aggregator (unresolved)"
    return None


# Per-ATS (tenant_pattern, job_id_pattern) pairs. `tenant` scopes the match
# instead of the `company` DB column: company text isn't populated until
# scoring (well after duplicate detection needs to run, see dedup.py), and
# even once populated the same employer shows up under several spellings
# ("BNY" vs "BNY (The Bank of New York Mellon)") that would need fuzzy
# matching to unify. The URL's own tenant/org segment is available the
# instant application_url resolves during enrichment and needs no
# normalization -- each ATS scopes its own tenant namespace, so two
# different real employers can never collide on (ats, tenant, job_id).
#
# Deliberately no generic "trailing numeric path segment" fallback for
# unrecognized platforms: verified against production data that two
# unrelated companies' career sites can coincidentally land on the same
# bare number (an IBM and a Schneider Electric posting both ended in
# "131307"). Only platforms below have a URL structure specific enough to
# trust; everything else returns None and falls back to text-based dedup.
_JOB_ID_PATTERNS: dict[str, tuple[str, str]] = {
    "Workday":    (r"https?://([^./]+)\.[^/]*myworkday", r"_((?:JR|R)-?[\w-]*\d[\w-]*)(?:\?|$)"),
    "Greenhouse": (r"[?&]for=([\w-]+)", r"[?&](?:token|gh_jid)=(\d+)"),
    "Oracle HCM": (r"/sites/([\w-]+)/job/", r"/job/(\d+)|[?&]jobId=(\d+)"),
    "iCIMS":      (r"https?://([^./]+)\.icims", r"/jobs/(\d+)|[?&]jobId=(\d+)"),
    "Dayforce":   (r"/en-[\w-]+/([\w-]+)/CANDIDATEPORTAL", r"/jobs/(\d+)"),
    "BambooHR":   (r"https?://([^./]+)\.bamboohr", r"/careers/(\d+)"),
    "Lever":      (r"lever\.co/([\w-]+)/", r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"),
    "Ashby":      (r"ashbyhq\.com/([\w-]+)/", r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"),
    "SmartRecruiters": (r"/company/([\w-]+)/", r"/publication/([0-9a-fA-F-]{20,})"),
    "Taleo":      (r"https?://([^./]+)\.taleo", r"[?&]job=(\d+)"),
    "Paylocity":  (r"https?://([^./]+)\.paylocity", r"/Details/(\d+)"),
    "Workable":   (r"workable\.com/([\w-]+)/", r"/j/([A-Za-z0-9]+)"),
    "Jobvite":    (r"jobvite\.com/([\w-]+)/", r"/job/([A-Za-z0-9]+)"),
    "BrassRing":  (r"[?&]siteid=(\d+)", r"[?&]jobid=(\d+)"),
    "ADP":        (r"[?&]cid=([\w-]+)", r"[?&]jobId=(\d+)"),
}


def extract_job_id(ats: str | None, url: str | None) -> tuple[str, str] | None:
    """(tenant, job_id) for a known, well-structured ATS platform's URL.

    Returns None for unrecognized platforms/aggregators, or when a pattern
    for a matched platform doesn't fire (e.g. a Greenhouse embed URL with
    no `for=` slug). Callers should treat None as "can't safely dedupe by
    ID here" and fall back to text-based matching, not as "not a duplicate".
    """
    if not ats or not url:
        return None
    patterns = _JOB_ID_PATTERNS.get(ats)
    if not patterns:
        return None
    tenant_pat, id_pat = patterns
    tenant_m = re.search(tenant_pat, url, re.I)
    id_m = re.search(id_pat, url, re.I)
    if not tenant_m or not id_m:
        return None
    tenant = tenant_m.group(1).lower()
    job_id = next(g for g in id_m.groups() if g)
    return tenant, job_id


def is_hard_to_automate(ats: str | None) -> bool:
    """Whether this ATS is known to defeat generic form-filling.

    These use custom widgets rather than native HTML controls, so a generic
    agent cannot reliably set their fields regardless of model quality.
    """
    return ats in {"SAP SuccessFactors", "Oracle HCM", "Taleo", "iCIMS"}
