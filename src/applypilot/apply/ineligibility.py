"""Company-wide eligibility sweep, shared by two callers:

- The automated path: `launcher._clear_terminal_flags_on_grad_date_mismatch`,
  triggered when a live apply attempt hits a RESULT:FAILED:grad_date_mismatch.
- The manual path: POST /api/report-ineligible, for when the candidate learns
  about an ineligibility (e.g. a rejection email) some way the pipeline has no
  visibility into.

Kept out of `apply/launcher.py` (which also imports backends and browser
automation) so the HTTP handler in web/server.py can import just this,
instead of the whole apply-run machinery.
"""

import sqlite3

from applypilot.database import get_connection

# Above this many sibling internships, one company is running more than one
# program and a single ineligibility report stops being evidence about the
# rest. Mirrors launcher._SIBLING_SWEEP_MAX -- same reasoning, same number.
_SIBLING_SWEEP_MAX = 8


def sweep_company_siblings(
    job_url: str, company: str | None, company_tier: str | None, note: str,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Apply the company-wide part of an ineligibility finding to every other
    not-yet-applied internship at the same company.

    A small/non-tiered employer (one program, one form) gets its siblings
    hard-disqualified: `is_terminal_internship*` cleared, `requires_returning_student`
    set, `eligible = 'no'`. A large or tier-listed employer (Amazon, Google,
    Meta, NVIDIA -- many unrelated programs under one name) only gets its
    siblings softened to `eligible = 'unclear'` for a human look, since one
    program's ineligibility isn't evidence about the others and the standing
    rule is every big-tech posting still gets applied to.

    Scoped to rows still reachable by acquire_job() (apply_status IS NULL or
    'failed') -- no reason to touch a job already submitted or mid-attempt.
    """
    conn = conn or get_connection()
    if not company:
        return {"broad_employer": None, "siblings_softened": 0, "siblings_disqualified": 0}

    sibling_count = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE company = ? "
        "AND job_type = 'internship' AND url != ?",
        (company, job_url),
    ).fetchone()["n"]
    broad_employer = bool(company_tier) or sibling_count > _SIBLING_SWEEP_MAX

    if broad_employer:
        cur = conn.execute(
            """
            UPDATE jobs SET
                eligible = 'unclear',
                eligibility_reason = CASE
                    WHEN eligibility_reason IS NULL OR eligibility_reason = ''
                    THEN ?
                    ELSE eligibility_reason || ' ' || ?
                END
            WHERE company = ?
              AND job_type = 'internship'
              AND url != ?
              AND eligible = 'yes'
              AND (apply_status IS NULL OR apply_status = 'failed')
            """,
            (note, note, company, job_url),
        )
        softened, disqualified = cur.rowcount, 0
    else:
        cur = conn.execute(
            """
            UPDATE jobs SET
                is_terminal_internship = 'no',
                is_terminal_internship_likely = 'no',
                requires_returning_student = 'yes',
                eligible = 'no',
                eligibility_reason = CASE
                    WHEN eligibility_reason IS NULL OR eligibility_reason = ''
                    THEN ?
                    ELSE eligibility_reason || ' ' || ?
                END
            WHERE company = ?
              AND job_type = 'internship'
              AND url != ?
              AND (apply_status IS NULL OR apply_status = 'failed')
            """,
            (note, note, company, job_url),
        )
        softened, disqualified = 0, cur.rowcount
    conn.commit()
    return {
        "broad_employer": broad_employer,
        "siblings_softened": softened,
        "siblings_disqualified": disqualified,
    }


def mark_ineligibility_and_sweep_company(
    job_url: str, note: str, conn: sqlite3.Connection | None = None,
) -> dict:
    """A human (or a live apply attempt) has confirmed this job is
    ineligible. Mark the job itself, then apply the same company-wide sweep
    the automated grad-date-mismatch path uses -- see sweep_company_siblings.
    """
    conn = conn or get_connection()
    note = note or "Reported ineligible."

    conn.execute(
        """
        UPDATE jobs SET
            eligible = 'no',
            requires_returning_student = 'yes',
            eligibility_reason = CASE
                WHEN eligibility_reason IS NULL OR eligibility_reason = ''
                THEN ?
                ELSE eligibility_reason || ' ' || ?
            END
        WHERE url = ?
        """,
        (note, note, job_url),
    )
    conn.commit()

    row = conn.execute(
        "SELECT company, company_tier FROM jobs WHERE url = ?", (job_url,)
    ).fetchone()
    company = row["company"] if row else None
    company_tier = row["company_tier"] if row and "company_tier" in row.keys() else None

    result = sweep_company_siblings(job_url, company, company_tier, note, conn=conn)
    return {"job_marked": True, "company": company, **result}
