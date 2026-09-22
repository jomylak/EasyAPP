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
# Platform patterns come first; anything they can't read (or any employer site
# with no pattern here) falls through to _generic_job_id below. That fallback is
# safe against the collision the original comment worried about (an IBM and a
# Schneider posting both ending in "131307") because the id is always scoped by
# the URL's own host and dedup additionally requires an exact title match.
_JOB_ID_PATTERNS: dict[str, tuple[str, str]] = {
    # Workday reqs aren't always JR-/R- prefixed (Monument Health's is "27_1439"),
    # and every repost of the same req appends "-1", "-2"... to the URL slug.
    "Workday":    (r"https?://([^./]+)\.[^/]*myworkday", r"/[^/_?#]+_([\w-]*\d[\w-]*)(?:\?|#|$)"),
    "Greenhouse": (r"[?&]for=([\w-]+)", r"[?&](?:token|gh_jid)=(\d+)"),
    "Oracle HCM": (r"/sites/([\w-]+)/job/", r"/job/(\d+)|[?&]jobId=(\d+)"),
    "iCIMS":      (r"https?://([^./]+)\.icims", r"/jobs/(\d+)|[?&]jobId=(\d+)"),
    "Dayforce":   (r"/en-[\w-]+/([\w-]+)/CANDIDATEPORTAL", r"/jobs/(\d+)"),
    "BambooHR":   (r"https?://([^./]+)\.bamboohr", r"/careers/(\d+)"),
    "Lever":      (r"lever\.co/([\w-]+)/", r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"),
    "Ashby":      (r"ashbyhq\.com/([\w-]+)/", r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"),
    "SmartRecruiters": (r"/company/([\w-]+)/", r"/publication/([0-9a-fA-F-]{20,})"),
    # Taleo has two URL generations: the classic careersection/jobdetail.ftl
    # one (tenant in the subdomain, id in ?job=) and a newer v2
    # viewRequisition one, where tenant and id are BOTH query params (org=,
    # rid=) -- the host there is a shared facility ("phg.tbe.taleo.net")
    # serving multiple unrelated employers, so using it as tenant would wrongly
    # scope two different companies' ids into the same namespace.
    "Taleo":      (r"[?&]org=([\w-]+)|https?://([^./]+)\.taleo", r"[?&]job=(\d+)|[?&]rid=(\d+)"),
    "Paylocity":  (r"https?://([^./]+)\.paylocity", r"/Details/(\d+)"),
    "Workable":   (r"workable\.com/([\w-]+)/", r"/j/([A-Za-z0-9]+)"),
    "Jobvite":    (r"jobvite\.com/([\w-]+)/", r"/job/([A-Za-z0-9]+)"),
    "BrassRing":  (r"[?&]siteid=(\d+)", r"[?&]jobid=(\d+)"),
    "ADP":        (r"[?&]cid=([\w-]+)", r"[?&]jobId=(\d+)"),
}


_ID_QUERY_PARAMS = ("jobid", "job_id", "reqid", "req_id", "requisitionid", "opportunityid",
                    "jobreqid", "positionid", "gh_jid", "jid")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HOST_NOISE = re.compile(r"^(?:www|careers?|jobs?|apply|external|search|hire|recruiting\d*)\.", re.I)


def _generic_job_id(url: str) -> tuple[str, str] | None:
    """(host, id) read off an employer/ATS URL with no dedicated pattern.

    Looks at well-known id query params first, then at path (and #fragment)
    segments that are unmistakably identifiers: long digit runs, UUIDs, or
    uppercase requisition codes -- never a title slug.
    """
    from urllib.parse import parse_qsl, unquote, urlsplit
    parts = urlsplit(url)
    host = _HOST_NOISE.sub("", parts.netloc.lower())
    if not host or host.endswith("jobright.ai"):
        return None
    for k, v in parse_qsl(parts.query, keep_blank_values=False):
        if k.lower() in _ID_QUERY_PARAMS and re.search(r"\d", v):
            return host, v.lower()
    found = None
    for seg in (parts.path + "/" + parts.fragment).split("/"):
        seg = unquote(seg).strip()
        if (re.fullmatch(r"\d{4,}", seg) or _UUID.match(seg)
                or (re.fullmatch(r"[A-Z0-9_-]{8,}", seg) and len(re.findall(r"\d", seg)) >= 4)):
            found = seg.lower()
    if not found:
        # A vanity career-site slug that embeds its id as a suffix instead of
        # a bare path segment, e.g. ".../north-chicago-il-jid-32205".
        m = re.search(r"-(?:jid|id|req)-(\d{4,})(?:[?#]|$)", parts.path, re.I)
        if m:
            found = m.group(1)
    return (host, found) if found else None


def extract_job_id(ats: str | None, url: str | None) -> tuple[str, str] | None:
    """(tenant, job_id): the employer's own posting id read from its URL.

    Uses the platform's pattern when the ATS is recognized, else a generic
    reader. Returns None when no identifier can be found; callers treat that
    as "can't dedupe by id", never as "not a duplicate". Workday's repost
    suffix ("-1") is stripped so every re-listing of one req shares one id.
    """
    if not url:
        return None
    result = None
    patterns = _JOB_ID_PATTERNS.get(ats or "")
    if patterns:
        tenant_pat, id_pat = patterns
        tenant_m = re.search(tenant_pat, url, re.I)
        id_m = re.search(id_pat, url, re.I)
        if tenant_m and id_m:
            tenant = next(g for g in tenant_m.groups() if g)
            result = (tenant.lower(), next(g for g in id_m.groups() if g))
    if result is None:
        result = _generic_job_id(url)
    if result is None:
        return None
    tenant, job_id = result
    if ats == "Workday" or "myworkday" in url:
        stripped = re.sub(r"-\d{1,2}$", "", job_id)
        if re.search(r"\d", stripped):
            job_id = stripped
    return tenant, job_id


def job_key(ats: str | None, url: str | None) -> str | None:
    """"tenant:job_id" -- the stored ats_job_id form."""
    r = extract_job_id(ats, url)
    return f"{r[0]}:{r[1]}" if r else None


def is_hard_to_automate(ats: str | None) -> bool:
    """Whether this ATS is known to defeat generic form-filling.

    These use custom widgets rather than native HTML controls, so a generic
    agent cannot reliably set their fields regardless of model quality.
    """
    return ats in {"SAP SuccessFactors", "Oracle HCM", "Taleo", "iCIMS"}
