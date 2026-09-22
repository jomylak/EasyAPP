from datetime import date

from applypilot.apply.gmail_scan import _sane_deadline, find_job

JOBS = [
    {"company": "IBM", "title": "Data Engineer Intern 2027"},
    {"company": "IBM", "title": "Back End Developer Intern 2027 - Dallas"},
    {"company": "Plaid", "title": "Software Engineer, New Grad"},
]


def test_deadline_guard():
    sent = date(2026, 9, 18)
    assert _sane_deadline("2026-09-25", sent) == "2026-09-25"
    assert _sane_deadline("2026-09-01", sent) is None   # before the email existed
    assert _sane_deadline("2027-06-01", sent) is None   # implausibly far
    assert _sane_deadline("soon", sent) is None


def test_find_job():
    assert find_job(JOBS, "Plaid, Inc.", None)[0] is JOBS[2]              # lone company match
    assert find_job(JOBS, "IBM", "Back End Developer Intern")[0] is JOBS[1]
    assert find_job(JOBS, "IBM", "Hacker Intern") == (None, True)         # several IBM roles, no match
    two = [{"company": "Roblox", "title": "A", "applied_at": "2026-09-01"}, {"company": "Roblox", "title": "B", "applied_at": "2026-09-05"}]
    assert find_job(two, "Roblox", None)[0] is two[1]                     # no role named -> latest application
    assert find_job([{"company": "Capgemini", "title": "x"}], "P&G", "y") == (None, False)  # "pg" is not inside "capgemini"
    assert find_job(JOBS, "Stripe", "SWE") == (None, False)               # unknown -> hand-applied
