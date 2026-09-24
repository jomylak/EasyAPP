"""Tests for dedup.link: strict duplicates, visible related groups, newest-visible."""

import pytest

from applypilot import dedup
from applypilot.database import init_db

DESC = "Build things with Python and SQL for the data platform team. " * 4
OTHER = "Design and deploy machine learning applications on the cloud. " * 4
ORACLE = ("https://x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/LazardStudentCareers"
          "/job/6606?jr_id={}")


def _insert(conn, url, **cols):
    row = {"url": url, "title": "Data Engineer Intern", "location": "New York, NY",
           "description": "blurb " * 10, "company": "Lazard",
           "company_normalized": "lazard", "discovered_at": "2026-09-01T00:00:00+00:00"}
    row.update(cols)
    conn.execute(f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))
    conn.commit()


def _row(conn, url):
    return conn.execute("SELECT duplicate_of, group_id, fit_score, full_description, "
                        "reported_ineligible_at FROM jobs WHERE url = ?", (url,)).fetchone()


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.db")


def test_reposted_ats_job_id_keeps_newest_visible_and_never_cycles(db):
    _insert(db, "old", ats="Oracle HCM", application_url=ORACLE.format("a"),
            posted_date="2026-09-01T00:00:00+00:00")
    dedup.link(db, "old")
    _insert(db, "new", ats="Oracle HCM", application_url=ORACLE.format("b"),
            posted_date="2026-09-15T00:00:00+00:00")
    dedup.link(db, "new")
    assert _row(db, "old")["duplicate_of"] == "new"
    assert _row(db, "new")["duplicate_of"] is None
    # Re-running any row (e.g. after a rebuild) must not flip anything.
    dedup.link(db, "old")
    dedup.link(db, "new")
    assert _row(db, "old")["duplicate_of"] == "new"
    assert _row(db, "new")["duplicate_of"] is None
    assert _row(db, "old")["group_id"] == _row(db, "new")["group_id"] is not None


def test_reported_ineligible_carries_onto_a_newer_repost(db):
    # A repost that shows up AFTER the manual flag was set normally wins
    # _recency and becomes the new visible rep -- the flag must travel with
    # it, or "report ineligible" would silently stop working after one repost.
    _insert(db, "old", ats="Oracle HCM", application_url=ORACLE.format("a"),
            posted_date="2026-09-01T00:00:00+00:00")
    dedup.link(db, "old")
    db.execute("UPDATE jobs SET reported_ineligible_at = ? WHERE url = ?",
               ("2026-09-02T00:00:00+00:00", "old"))
    db.commit()
    _insert(db, "new", ats="Oracle HCM", application_url=ORACLE.format("b"),
            posted_date="2026-09-15T00:00:00+00:00")
    dedup.link(db, "new")
    assert _row(db, "new")["reported_ineligible_at"] == "2026-09-02T00:00:00+00:00"
    assert _row(db, "new")["duplicate_of"] is None
    assert _row(db, "old")["duplicate_of"] == "new"


def test_same_ats_id_but_different_title_is_not_a_duplicate(db):
    _insert(db, "a", ats="Oracle HCM", application_url=ORACLE.format("a"))
    _insert(db, "b", ats="Oracle HCM", application_url=ORACLE.format("b"), title="AI Engineer Intern")
    dedup.link(db, "a")
    dedup.link(db, "b")
    assert _row(db, "a")["duplicate_of"] is None and _row(db, "b")["duplicate_of"] is None


def test_similar_titles_at_same_company_are_separate_jobs(db):
    # The Lazard case: two different roles you apply to separately.
    _insert(db, "de", full_description=DESC, title="2027 Data Engineer Summer Internship")
    _insert(db, "ai", full_description=DESC + "AI", title="2027 AI Engineer Summer Internship")
    dedup.link(db, "de")
    dedup.link(db, "ai")
    for u in ("de", "ai"):
        assert _row(db, u)["duplicate_of"] is None and _row(db, u)["group_id"] is None


def test_identical_text_but_different_ats_requisitions_stay_separate(db):
    _insert(db, "a", full_description=DESC, ats="Oracle HCM", application_url=ORACLE.format("a"))
    _insert(db, "b", full_description=DESC, ats="Oracle HCM",
            application_url=ORACLE.format("b").replace("/job/6606", "/job/7777"))
    dedup.link(db, "a")
    dedup.link(db, "b")
    assert _row(db, "a")["duplicate_of"] is None and _row(db, "b")["duplicate_of"] is None
    assert _row(db, "a")["group_id"] is None


def test_exact_text_same_company_title_location_is_duplicate(db):
    _insert(db, "a", full_description=DESC, discovered_at="2026-09-01T00:00:00+00:00")
    _insert(db, "b", full_description=DESC, discovered_at="2026-09-05T00:00:00+00:00")
    dedup.link(db, "a")
    dedup.link(db, "b")
    assert _row(db, "a")["duplicate_of"] == "b"


def test_same_title_different_description_is_left_alone(db):
    _insert(db, "a", full_description=DESC, description="first blurb " * 8)
    _insert(db, "b", full_description=OTHER, description="second blurb " * 8)
    dedup.link(db, "a")
    dedup.link(db, "b")
    assert _row(db, "a")["group_id"] is None and _row(db, "b")["group_id"] is None


def test_same_posting_in_other_cities_is_grouped_despite_location_lines(db):
    _insert(db, "a", full_description=DESC + "Location: New York, NY.", location="New York, NY")
    _insert(db, "b", full_description=DESC + "Location: Chicago, IL.", location="Chicago, IL")
    _insert(db, "c", full_description=DESC + "Location: Austin, TX.", location="Austin, TX")
    for u in ("a", "b", "c"):
        dedup.link(db, u)
    assert all(_row(db, u)["duplicate_of"] is None for u in ("a", "b", "c"))
    assert _row(db, "a")["group_id"] == _row(db, "b")["group_id"] == _row(db, "c")["group_id"] is not None


def test_never_matches_across_companies(db):
    _insert(db, "a", full_description=DESC)
    _insert(db, "b", full_description=DESC, company="Other Co", company_normalized="other")
    dedup.link(db, "a")
    dedup.link(db, "b")
    assert _row(db, "a")["group_id"] is None and _row(db, "b")["duplicate_of"] is None


def test_discovery_time_repost_does_not_inherit_before_its_own_enrichment(db):
    # Regression: dedup.link runs at discovery time too (smartextract.py's
    # _safe_link), before enrichment ever gets a chance to scrape a brand-new
    # repost. A same-blurb match against an already-enriched older row used
    # to merge here and silently inherit that older row's (possibly
    # weeks-stale) full_description/score -- confirmed live on a Booz Allen
    # posting whose graduation-window language had since changed. Both must
    # stay visible and un-merged until "new" gets its own full_description.
    _insert(db, "old", full_description=DESC, fit_score=9,
            discovered_at="2026-09-01T00:00:00+00:00")
    _insert(db, "new", discovered_at="2026-09-10T00:00:00+00:00")  # same blurb, not enriched
    result = dedup.link(db, "new")
    assert result["duplicate_of"] is None
    assert _row(db, "old")["duplicate_of"] is None
    new = _row(db, "new")
    assert new["fit_score"] is None and new["full_description"] is None

    # Once "new" gets its own real scrape (identical text -- a genuine
    # repost), re-running link() now correctly merges them full-to-full.
    db.execute("UPDATE jobs SET full_description = ? WHERE url = 'new'", (DESC,))
    db.commit()
    dedup.link(db, "new")
    assert _row(db, "old")["duplicate_of"] == "new"


def test_backfill_rebuilds_from_scratch(db):
    _insert(db, "a", full_description=DESC, discovered_at="2026-09-01T00:00:00+00:00")
    _insert(db, "b", full_description=DESC, discovered_at="2026-09-05T00:00:00+00:00")
    db.execute("UPDATE jobs SET duplicate_of = 'a' WHERE url = 'a'")  # stale/corrupt link
    db.commit()
    stats = dedup.backfill(db)
    assert _row(db, "a")["duplicate_of"] == "b" and _row(db, "b")["duplicate_of"] is None
    assert stats["duplicates_found"] == 1


def test_title_normalization_is_only_case_space_and_dash():
    assert dedup.norm_title("  Data  Engineer – Intern ") == dedup.norm_title("data engineer - intern")
    assert dedup.norm_title("Data Engineer Intern") != dedup.norm_title("Software Engineer Intern")


def test_canonicalize_url_strips_tracking_but_keeps_ids():
    assert dedup.canonicalize_url("https://a.com/x?utm_source=z&token=5") == "https://a.com/x?token=5"


def test_normalize_location_collapses_blank():
    assert dedup.normalize_location("  ") is None and dedup.normalize_location("NYC") == "NYC"


def test_self_reported_application_shows_beside_the_real_posting(db):
    _insert(db, "real", full_description=DESC, title="2027 Data Engineer Summer Internship")
    _insert(db, "self-reported:lazard:x", title="2027 Data Engineer Summer Internship",
            company="Lazard", company_normalized=None, location=None, description=None,
            apply_status="applied", apply_backend="manual")
    dedup.link(db, "real")
    dedup.link(db, "self-reported:lazard:x")
    assert _row(db, "real")["group_id"] == _row(db, "self-reported:lazard:x")["group_id"] is not None
    assert _row(db, "real")["duplicate_of"] is None


def test_workday_repost_suffix_and_non_prefixed_req_ids_resolve_to_one_job(db):
    base = "https://monumenthealth.wd1.myworkdayjobs.com/{}/job/Rapid-City-SD-USA/Cybersecurity-Engineer-I_27_1439{}?jr_id={}"
    _insert(db, "a", ats="Workday", application_url=base.format("Engagement", "", "a"),
            full_description=DESC, posted_date="2026-09-07T00:00:00+00:00")
    _insert(db, "b", ats="Workday", application_url=base.format("Goldcareers", "-1", "b"),
            full_description=OTHER, posted_date="2026-09-12T00:00:00+00:00")  # text differs, id doesn't
    dedup.link(db, "a")
    dedup.link(db, "b")
    assert _row(db, "a")["duplicate_of"] == "b" and _row(db, "b")["duplicate_of"] is None


def test_link_does_not_commit_inside_a_callers_transaction(db):
    # acquire_job calls link() from inside its own BEGIN IMMEDIATE claim
    # transaction. A commit here would drop that lock early and let a second
    # worker claim the same row -- see dedup.link's docstring comment.
    _insert(db, "solo")
    db.execute("BEGIN IMMEDIATE")
    dedup.link(db, "solo")
    assert db.in_transaction
    db.rollback()


def test_generic_employer_id_extraction():
    from applypilot.ats import job_key
    assert job_key(None, "https://www.amazon.jobs/en/jobs/10412530/sde-intern?cmpid=X") == "amazon.jobs:10412530"
    assert job_key(None, "https://careers.ibm.com/en_US/careers/JobDetail?jobId=130762&src=jobright") == "ibm.com:130762"
    assert job_key(None, "https://jobs.spectrum.com/job/x/slug/4673/100133781152?jr_id=z") == "spectrum.com:100133781152"
    assert job_key(None, "https://jobright.ai/jobs/info/6aad6f082e757fcb5c8b85ad") is None
