from applypilot.apply.post_apply_status import parse_new_job_lines, parse_scan_results


def test_parse_scan_results_happy_path():
    output = (
        "Some reasoning text.\n"
        "RESULT:1|oa|2026-09-20|HackerRank assessment invite\n"
        "more text\n"
        "RESULT:3|rejected||Thanks for applying, we've moved on\n"
    )
    assert parse_scan_results(output, job_count=3) == [
        (1, "oa", "2026-09-20", "HackerRank assessment invite"),
        (3, "rejected", None, "Thanks for applying, we've moved on"),
    ]


def test_parse_scan_results_ignores_bad_status_out_of_range_index_and_bad_date():
    output = (
        "RESULT:1|made_up_status||x\n"
        "RESULT:9|offer||out of range\n"
        "RESULT:2|interview|next Tuesday|bad date format\n"
    )
    assert parse_scan_results(output, job_count=3) == [
        (2, "interview", None, "bad date format"),
    ]


def test_parse_scan_results_no_matches():
    assert parse_scan_results("no results here", job_count=5) == []


def test_parse_new_job_lines_happy_path():
    output = (
        "NEWJOB:Acme Corp|Software Engineer Intern|https://acme.com/careers/123|2026-09-10\n"
        "NEWJOB:Beta Inc|unknown|none|unknown\n"
    )
    assert parse_new_job_lines(output) == [
        ("Acme Corp", "Software Engineer Intern", "https://acme.com/careers/123", "2026-09-10"),
        ("Beta Inc", None, None, None),
    ]


def test_parse_new_job_lines_requires_company():
    assert parse_new_job_lines("NEWJOB:|Some Title|none|none\n") == []
