from applypilot.apply.post_apply_status import parse_scan_results


def test_parse_scan_results_happy_path():
    output = (
        "Some reasoning text.\n"
        "RESULT:1|oa|HackerRank assessment invite\n"
        "more text\n"
        "RESULT:3|rejected|Thanks for applying, we've moved on\n"
    )
    assert parse_scan_results(output, job_count=3) == [
        (1, "oa", "HackerRank assessment invite"),
        (3, "rejected", "Thanks for applying, we've moved on"),
    ]


def test_parse_scan_results_ignores_bad_status_and_out_of_range_index():
    output = "RESULT:1|made_up_status|x\nRESULT:9|offer|out of range\n"
    assert parse_scan_results(output, job_count=3) == []


def test_parse_scan_results_no_matches():
    assert parse_scan_results("no results here", job_count=5) == []
