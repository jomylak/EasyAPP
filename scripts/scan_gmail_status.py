"""Scan Gmail (inbox + spam) for post-apply status updates on applied jobs.

Drives a single headless ``goose run`` with only the ``gmail`` MCP extension
(no browser) on a free OpenRouter model -- this is read-and-classify, not
worth spending the apply flow's paid model on. Goose does both the searching
and the classification itself, so there's no separate LLM call. Given a
numbered list of recently-applied jobs it searches for matching emails and
prints one ``RESULT:<n>|status|evidence`` line per job it can resolve;
anything it can't confidently match is left alone for the next pass rather
than guessed at (see [[applypilot-known-quirks]] for why goose output is
parsed by marker line rather than trusted structured output).

A job whose ``post_apply_source`` is already 'manual' is never touched --
that is the human's correction. See apply/post_apply_status.py for the
status vocabulary.

Usage:
    python scripts/scan_gmail_status.py            # newest gmail_scan_batch_size unresolved jobs
    python scripts/scan_gmail_status.py --backfill  # every applied job with no status yet, no cap
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import load_env, load_settings, DEFAULTS, ensure_dirs

load_env()
ensure_dirs()

from applypilot.database import init_db  # noqa: E402
from applypilot.apply.post_apply_status import parse_scan_results  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _candidate_jobs(conn, backfill: bool, limit: int) -> list[dict]:
    query = """
        SELECT url, title, company, applied_at FROM jobs
        WHERE apply_status IN ('applied', 'manual')
          AND applied_at IS NOT NULL
          AND (post_apply_status IS NULL OR post_apply_status = 'none')
          AND (post_apply_source IS NULL OR post_apply_source != 'manual')
        ORDER BY applied_at DESC
    """
    if not backfill:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def _build_prompt(jobs: list[dict]) -> str:
    lines = [
        "You have Gmail search/read access (mcp tools prefixed gmail__). "
        "Do NOT send, draft, delete, or modify any email or label -- read-only.",
        "",
        "Below is a numbered list of job applications. For each one, search Gmail "
        "-- including the spam folder (in:spam) -- for emails from or about that "
        "employer's hiring process, sent on or after the application date. "
        "Classify what you find into exactly one of: oa (online assessment/coding "
        "test invite), interview (interview request/scheduling), rejected "
        "(rejection/not moving forward), offer (job offer). If a job's most recent "
        "email implies more than one stage (e.g. OA then interview), report the "
        "furthest stage reached.",
        "",
        "For every job you can confidently resolve, print one line exactly in this "
        "form (no other text on the line):",
        "RESULT:<number>|<status>|<short evidence, e.g. subject line, under 80 chars>",
        "",
        "Skip (print nothing for) any job you find no matching email for, or aren't "
        "confident about. Don't guess.",
        "",
        "Jobs:",
    ]
    for i, job in enumerate(jobs, 1):
        lines.append(
            f"{i}. company={job.get('company') or 'unknown'!r} "
            f"title={job.get('title') or ''!r} applied_at={job.get('applied_at')}"
        )
    return "\n".join(lines)


# goose's own marker for "an API call in this session failed", printed to
# stdout with returncode 0 -- e.g. a retired/unknown model id, a 429, quota
# exhausted. Without checking for this, a dead model id would silently
# report "resolved=0" every day forever instead of falling back.
_GOOSE_ERROR_MARKER = "Ran into this error"


def _goose_bin() -> str:
    # This script runs under the pipeline daemon, not the web server -- whose
    # systemd unit's PATH is narrower and doesn't include ~/.local/bin, where
    # `goose install` puts the binary. shutil.which() alone would silently
    # fail there, so it's not enough.
    return shutil.which("goose") or str(Path.home() / ".local" / "bin" / "goose")


def _run_goose_once(prompt: str, provider: str, model: str) -> tuple[str, int, bool]:
    """One goose session. Returns (text output, gmail tool-call count, ok).

    ``ok`` is false on a nonzero exit, a timeout, or goose's own
    mid-session error marker -- the caller uses that to decide whether to
    retry on the fallback provider.
    """
    cmd = [
        _goose_bin(), "run",
        "--no-session", "--no-profile",
        "-i", "-",
        "--provider", provider,
        "--model", model,
        "--max-turns", "60",
        "--output-format", "stream-json",
        "--with-extension", "gmail:npx -y @gongrzhe/server-gmail-autoauth-mcp",
    ]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # goose's "google" provider reads GOOGLE_API_KEY, not GEMINI_API_KEY
    # (which is what the rest of ApplyPilot's .env and llm.py use).
    if provider == "google" and not env.get("GOOGLE_API_KEY", "").strip():
        env["GOOGLE_API_KEY"] = env.get("GEMINI_API_KEY", "")
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               env=env, timeout=1200)
    except subprocess.TimeoutExpired:
        logger.warning("goose (%s/%s) timed out after 1200s", provider, model)
        return "", 0, False

    text_parts: list[str] = []
    tool_calls = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "message":
            continue
        for block in msg.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "text":
                text_parts.append(block.get("text", ""))
            elif bt == "toolRequest":
                tool_calls += 1
    output = "".join(text_parts)

    ok = proc.returncode == 0 and _GOOSE_ERROR_MARKER not in proc.stdout
    if not ok:
        logger.warning("goose (%s/%s) exited %d: %s", provider, model, proc.returncode,
                        (proc.stderr or proc.stdout)[-2000:])
    return output, tool_calls, ok


def _run_goose(prompt: str, settings: dict) -> tuple[str, int, str]:
    """Runs the primary provider, falling back to a free OpenRouter model on
    any failure. Returns (text output, gmail tool-call count, provider used).
    """
    provider = settings.get("gmail_scan_provider") or DEFAULTS["gmail_scan_provider"]
    model = settings.get("gmail_scan_model") or DEFAULTS["gmail_scan_model"]
    output, tool_calls, ok = _run_goose_once(prompt, provider, model)
    if ok:
        return output, tool_calls, provider

    fb_provider = settings.get("gmail_scan_fallback_provider") or DEFAULTS["gmail_scan_fallback_provider"]
    fb_model = settings.get("gmail_scan_fallback_model") or DEFAULTS["gmail_scan_fallback_model"]
    logger.warning("Falling back to %s/%s", fb_provider, fb_model)
    output, tool_calls, ok = _run_goose_once(prompt, fb_provider, fb_model)
    return output, tool_calls, fb_provider if ok else f"{fb_provider} (failed)"


def main() -> None:
    backfill = "--backfill" in sys.argv[1:]
    conn = init_db()
    settings = load_settings()
    batch_size = settings.get("gmail_scan_batch_size") or DEFAULTS["gmail_scan_batch_size"]

    jobs = _candidate_jobs(conn, backfill, batch_size)
    if not jobs:
        print("SCAN DONE resolved=0 candidates=0", flush=True)
        return

    output, tool_calls, provider_used = _run_goose(_build_prompt(jobs), settings)

    now = datetime.now(timezone.utc).isoformat()
    resolved = 0
    for idx, status, evidence in parse_scan_results(output, len(jobs)):
        job = jobs[idx - 1]
        conn.execute(
            "UPDATE jobs SET post_apply_status = ?, post_apply_status_at = ?, "
            "post_apply_evidence = ?, post_apply_source = 'gmail' WHERE url = ?",
            (status, now, evidence, job["url"]),
        )
        resolved += 1
    conn.commit()
    # tool_calls is every gmail__* call goose made this session (searches +
    # message reads combined) -- the actual LLM request count is tool_calls+1
    # (one inference per tool result, same accounting as the apply backend;
    # see goose.py's run_job), not one call per job or per email.
    print(f"SCAN DONE resolved={resolved} candidates={len(jobs)} "
          f"provider={provider_used} gmail_tool_calls={tool_calls} "
          f"llm_requests={tool_calls + 1}", flush=True)


if __name__ == "__main__":
    main()
