"""Re-score the rows that predate the TERM / TERMINAL_EVIDENCE prompt checks.

Uses whatever provider .env already configures -- GLM via OpenRouter
(glm-5.3-flash), ~15s/call. Gemini's free tier caps around 1,000 rows/day and
isn't worth the rate-limit risk for an unattended multi-hour run.
"""
import logging
import sys

from applypilot.config import load_env, ensure_dirs

load_env()
ensure_dirs()

from applypilot.database import init_db  # noqa: E402

init_db()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from applypilot.scoring.scorer import run_scoring  # noqa: E402

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
result = run_scoring(limit=limit, stale_only=True)
print(f"RESCORE DONE scored={result['scored']} errors={result['errors']} "
      f"elapsed={result['elapsed']:.0f}s", flush=True)
