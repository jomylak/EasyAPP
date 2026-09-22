"""Scan Gmail (inbox + spam) for post-apply status (OA / interview / rejected /
offer, with the email's stated deadline) and for jobs applied to by hand.

Incremental: only emails not yet in ``gmail_seen`` are fetched and classified,
so it is cheap to run every couple of hours. Logic lives in
``applypilot.apply.gmail_scan``.

Usage:
    python scripts/scan_gmail_status.py                # last 14 days, unseen emails only
    python scripts/scan_gmail_status.py --rebuild 60   # wipe gmail-sourced statuses, rescan 60 days
"""
import logging
import sys
from datetime import date, timedelta

from applypilot.config import load_env, ensure_dirs

load_env()
ensure_dirs()

from applypilot.apply.gmail_scan import rebuild, scan  # noqa: E402
from applypilot.database import init_db  # noqa: E402
from applypilot.llm import get_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    argv = sys.argv[1:]
    conn = init_db()
    days = 14
    if "--rebuild" in argv:
        days = int(argv[argv.index("--rebuild") + 1])
        rebuild(conn)
    print("SCAN DONE", scan(conn, get_client(), date.today() - timedelta(days=days)), flush=True)
