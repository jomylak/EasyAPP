"""Regression checks for ats.py's detection and id-extraction gaps found by
auditing the live DB: the HTML-fingerprint tier was being fed extracted text
instead of real page HTML (so it never matched anything), and several real
employer URL shapes fell through _generic_job_id / the Taleo pattern."""

from applypilot.ats import detect_ats, extract_job_id


def test_html_fingerprint_tier_matches_real_markup():
    # A vanity domain (no URL pattern) that only reveals its ATS via markup --
    # this is the tier that was silently dead when fed description text.
    html = '<div class="grnhse_app"><script>window.grnhse_iframe = true;</script></div>'
    assert detect_ats("https://careers.example.com/job/123", html) == "Greenhouse"


def test_html_fingerprint_tier_does_not_match_plain_description_text():
    # Guards against regressing back to feeding it description text: prose
    # should never accidentally trip a markup fingerprint.
    text = "We are looking for a Greenhouse... I mean, a software engineer."
    assert detect_ats("https://careers.example.com/job/123", text) is None


def test_generic_job_id_four_digit_path_segment():
    assert extract_job_id(None, "https://careers.kindermorgan.com/careers-home/jobs/6032?jr_id=x") == (
        "kindermorgan.com", "6032",
    )
    assert extract_job_id(None, "https://careers.fm.com/careers-home/jobs/2029?jr_id=x") == ("fm.com", "2029")


def test_generic_job_id_slug_embedded_suffix():
    url = (
        "https://careers.abbvie.com/en/job/2027-business-technology-solutions-"
        "intern-cloud-engineering-undergraduate-in-north-chicago-il-jid-32205?jr_id=x"
    )
    assert extract_job_id(None, url) == ("abbvie.com", "32205")


def test_taleo_v2_org_and_rid_query_params():
    url = "https://phe.tbe.taleo.net/phe02/ats/careers/v2/viewRequisition?org=OLIN&cws=47&rid=15656&jr_id=x"
    assert extract_job_id("Taleo", url) == ("olin", "15656")


def test_taleo_classic_job_query_param_still_works():
    url = "https://textron.taleo.net/careersection/textron_ur/jobdetail.ftl?job=1540158&tz=UTC"
    assert extract_job_id("Taleo", url) == ("textron", "1540158")
