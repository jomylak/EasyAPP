from datetime import date, datetime, timezone

from applypilot.apply.gmail_scan import _sane_deadline, find_job, scan
from applypilot.database import init_db

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


class _FakeClient:
    def ask(self, prompt, **kwargs):
        return (
            '[{"id": "m1", "kind": "confirmation", "company": "Acme Corp", '
            '"title": "Backend Engineer", "deadline": null}]'
        )


def test_scan_flips_a_bot_failure_to_manual(tmp_path, monkeypatch):
    """A real confirmation email for a job the bot already gave up on must
    update that row in place, not spawn a duplicate self-reported:... row --
    the failure (apply_error) stays on the row instead of being lost."""
    conn = init_db(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO jobs (url, company, title, apply_status, apply_error, "
        "apply_error_category, apply_attempts) "
        "VALUES ('https://acme.com/j/1', 'Acme Corp', 'Backend Engineer', "
        "'failed', 'site_blocked', 'site_blocked', 3)"
    )
    conn.commit()

    sent = datetime(2026, 9, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "applypilot.apply.gmail_scan.fetch_emails",
        lambda since, seen: [{"id": "m1", "sent": sent, "from": "no-reply@acme.com",
                              "subject": "Thanks for applying", "body": "..."}],
    )

    stats = scan(conn, _FakeClient(), date(2026, 9, 1))

    row = conn.execute(
        "SELECT apply_status, apply_backend, apply_error FROM jobs WHERE url = 'https://acme.com/j/1'"
    ).fetchone()
    assert row["apply_status"] == "applied"
    assert row["apply_backend"] == "manual"
    assert row["apply_error"] == "site_blocked"  # the earlier bot failure stays visible
    assert stats["new_manual"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1  # no duplicate row
    v = conn.execute("SELECT kind, outcome, job_url FROM gmail_verdicts WHERE msg_id = 'm1'").fetchone()
    assert (v["outcome"], v["job_url"]) == ("matched_no_update", "https://acme.com/j/1")
