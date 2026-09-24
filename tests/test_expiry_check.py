"""Regression cases for expiry_check._heuristic, captured from real false
positives (Rippling, Viasat, SmartRecruiters/DataDome, GDMS/Azure WAF) and
real true positives (NASSCO, SmartRecruiters' own /expired page)."""

from applypilot.apply.expiry_check import _heuristic

# Trimmed excerpts of the actual bytes fetched -- enough to exercise the
# same branch, not full pages.

RIPPLING_OPEN = """
<!DOCTYPE html><html><head><title>Production Engineer</title>
<script>var errorStrings = {notFound: "Page not found", gone: "This job is no longer available"};</script>
</head><body><h1>Production Engineer</h1><p>Webull is hiring...</p>
<button>Apply now</button></body></html>
""" + "x" * 600

VIASAT_OPEN = """
<!DOCTYPE html><html><head><script>window.copy = "no longer available";</script></head>
<body><h1>Software Engineer - Automation, Early Career</h1><p>About us...</p></body></html>
""" + "x" * 600

SMARTRECRUITERS_DATADOME_WALL = (
    '<html><head><title>smartrecruiters.com</title></head><body>'
    '<p id="cmsg">Please enable JS and disable any ad blocker</p>'
    '<script>/* datadome challenge js */</script></body></html>'
)

GDMS_AZURE_WAF_WALL = (
    '<html><head><title>Azure WAF</title></head><body>'
    'Azure WAF Please enable JavaScript to run this application. '
    'An unexpected error occured.</body></html>'
)

NASSCO_EXPIRED = """
<html><body><h1>AI Engineer (All Levels)</h1>
<p>Sorry, this position has been filled.</p></body></html>
""" + "x" * 600

CLOSED_PHRASE_PAGE = """
<html><body><h1>Software Engineer</h1>
<p>This job is no longer accepting applications.</p></body></html>
""" + "x" * 600

# SmartRecruiters' real /expired redirect: 403, short body, no literal phrase
# match and no known bot-wall marker -- must fall through to the LLM tier
# rather than being silently treated as open.
SMARTRECRUITERS_EXPIRED_REDIRECT = (
    "<html><body><h1>This job ad has expired</h1>"
    "<p>Find more job offers at https://jobs.smartrecruiters.com</p></body></html>"
)


def test_open_job_with_generic_js_bundle_strings_is_not_flagged():
    assert _heuristic(200, RIPPLING_OPEN) == "open"
    assert _heuristic(200, VIASAT_OPEN) == "open"


def test_bot_challenge_walls_are_inconclusive_not_expired():
    assert _heuristic(403, SMARTRECRUITERS_DATADOME_WALL) == "inconclusive"
    assert _heuristic(403, GDMS_AZURE_WAF_WALL) == "inconclusive"


def test_genuinely_closed_listings_are_still_caught():
    assert _heuristic(200, NASSCO_EXPIRED) == "expired"
    assert _heuristic(403, CLOSED_PHRASE_PAGE) == "expired"


def test_bare_404_still_short_circuits():
    assert _heuristic(404, "") == "expired"


def test_unmatched_403_with_no_evidence_defers_to_llm_tier():
    # No phrase, no known bot marker -- must not be silently treated as
    # open OR expired by the free heuristic; only the LLM tier may decide.
    assert _heuristic(403, SMARTRECRUITERS_EXPIRED_REDIRECT) == "inconclusive"


if __name__ == "__main__":
    test_open_job_with_generic_js_bundle_strings_is_not_flagged()
    test_bot_challenge_walls_are_inconclusive_not_expired()
    test_genuinely_closed_listings_are_still_caught()
    test_bare_404_still_short_circuits()
    test_unmatched_403_with_no_evidence_defers_to_llm_tier()
    print("ok")
