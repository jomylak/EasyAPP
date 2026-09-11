"""Reset the jobs that failed for captcha/bot-detection reasons so they can
be re-run through the APPLY_PROXY test path.

These jobs were marked terminal (apply_attempts=99, see launcher.mark_result)
because captcha/site-block reasons are in outcomes.PERMANENT_FAILURES, so
they won't naturally re-enter the queue -- this is what lets them.

Usage: python scripts/reset_captcha_cohort.py [--dry-run]
"""
import sys

from applypilot.config import load_env, ensure_dirs

load_env()
ensure_dirs()

from applypilot.database import get_connection, init_db  # noqa: E402

init_db()

REASONS = (
    "captcha",
    "ashby_spam_detection",
    "employer_site_blocks_automation",
    "employer_site_blocked_by_waf",
    "indeed_blocks_automated_browsers",
)

dry_run = "--dry-run" in sys.argv
conn = get_connection()
placeholders = ",".join("?" for _ in REASONS)

rows = conn.execute(
    f"SELECT url, title, apply_error FROM jobs "
    f"WHERE apply_status = 'failed' AND apply_error IN ({placeholders})",
    REASONS,
).fetchall()

print(f"{len(rows)} jobs match the captcha/bot-detection cohort:")
for r in rows:
    print(f"  [{r['apply_error']}] {r['title'][:60]} -- {r['url']}")

if dry_run:
    print("\n--dry-run: no changes made.")
    sys.exit(0)

if not rows:
    print("\nNothing to reset.")
    sys.exit(0)

conn.execute(
    f"UPDATE jobs SET apply_status = NULL, apply_error = NULL, "
    f"apply_error_category = NULL, apply_attempts = 0, agent_id = NULL "
    f"WHERE apply_status = 'failed' AND apply_error IN ({placeholders})",
    REASONS,
)
conn.commit()
print(f"\nReset {len(rows)} jobs. They'll be picked up by the next apply run "
      f"that has APPLY_PROXY set (see .env.example).")
