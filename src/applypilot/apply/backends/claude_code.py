"""Claude Code apply backend.

Spawns a ``claude -p`` session per job with Playwright MCP pointed at the
worker's Chrome (via CDP) and Gmail MCP for verification codes. The agent
drives the whole application and prints a ``RESULT:`` line, which is scraped
back out of the streamed output.

This is the original ApplyPilot implementation, moved out of ``launcher`` when
the backend abstraction was introduced. Behaviour is unchanged.
"""

import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.ats import detect_ats
from applypilot.apply.chrome import get_worker_proxy, reset_worker_dir, _kill_process_tree
from applypilot.apply.dashboard import accumulate_usage, add_event, add_worker_action, get_state, update_state
from applypilot.scoring.router import resume_paths_for_job

logger = logging.getLogger(__name__)

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_stats: dict[int, dict] = {}  # worker_id -> last run's token accounting
_claude_lock = threading.Lock()

# Only these models write to the known-quirks cache. A weaker model's claim
# that a fallback "worked" isn't trustworthy without the same rigor a stronger
# model applies -- Haiku has fabricated RESULT:APPLIED with blank fields, so a
# self-reported fix from it could poison the cache for every future run on
# that platform. Cheap models still READ the cache; they just don't write it.
_TRUSTED_QUIRK_WRITERS = {"sonnet", "opus"}


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                    f"--output-dir={config.playwright_output_dir()}",
                    f"--output-max-size={config.DEFAULTS['playwright_output_max_bytes']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
            # Deterministic replacements for generic browser friction (file
            # upload, searchable comboboxes) -- see
            # apply/mcp_tools/server.py for the scope rule and tool list.
            "applytools": {
                "command": sys.executable,
                "args": [
                    "-m", "applypilot.apply.mcp_tools.server",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                ],
            },
        }
    }



def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    settings = config.load_settings()

    # Resume text -- routed live if this job never went through `run tailor`.
    txt_path, _pdf_path = resume_paths_for_job(job)
    resume_text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        proxy_string=get_worker_proxy(worker_id),
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", (
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 # The employer, falling back to the source board only when
                 # scoring has not filled it in yet -- `site` is the board
                 # ("Intern List - SWE"), never the company.
                 company=job.get("company") or job.get("site", ""),
                 company_tier=job.get("company_tier"),
                 url=job.get("url", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting",
                 recent_actions=[])
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        # New process group on Unix so _kill_process_tree (os.killpg) tears
        # down claude and its MCP-server children without also killing
        # whatever process spawned this one -- claude was inheriting our own
        # process group, so timing out a run could SIGKILL its own caller.
        popen_kwargs: dict = {}
        if platform.system() != "Windows":
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
            **popen_kwargs,
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        # Watchdog: if Chrome's DevTools port dies, the agent has no browser and
        # cannot recover -- but it does not know that, so it sits in
        # browser_wait_for until the whole run times out. A real run lost ~10
        # minutes that way. Kill the session promptly instead.
        cdp_dead = threading.Event()

        def _watch_cdp() -> None:
            import socket
            misses = 0
            while not cdp_dead.wait(10):
                if proc.poll() is not None:
                    return
                sock = socket.socket()
                try:
                    sock.settimeout(2)
                    sock.connect(("127.0.0.1", port))
                    misses = 0
                except OSError:
                    misses += 1
                    # Two consecutive misses -- one can be a transient stall
                    # while Chrome is busy, two means it is gone.
                    if misses >= 2:
                        logger.error("[worker-%d] Chrome DevTools port %d died; "
                                     "ending the session", worker_id, port)
                        cdp_dead.set()
                        _kill_process_tree(proc.pid)
                        return
                finally:
                    sock.close()

        watchdog = threading.Thread(target=_watch_cdp,
                                    name=f"cdp-watch-{worker_id}", daemon=True)
        watchdog.start()

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        navigated_urls: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                                # Unlike goose, each block here is already a
                                # complete turn's text (not one token per
                                # envelope), so it can go straight to the
                                # live log with no buffering.
                                stripped = block["text"].strip()
                                if stripped:
                                    add_worker_action(worker_id, f"\U0001f4ad {stripped[:200]}")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                    .replace("mcp__applytools__", "")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                    if name == "browser_navigate":
                                        navigated_urls.append(inp["url"])
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                                add_worker_action(worker_id, desc[:120])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=settings.get("apply_timeout") or config.DEFAULTS["apply_timeout"])
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        # Resolve the real ATS platform for this run. The stored URL is often
        # an aggregator redirect (Jobright, Intern List) that detect_ats can't
        # resolve -- but the agent's own browser_navigate calls reveal the
        # real destination once it follows the posting to the employer's ATS.
        # Check those before falling back to the stored (possibly unresolved)
        # URLs, so a successful run tells future runs what platform this job
        # actually lives on.
        job_ats = None
        for nav_url in navigated_urls:
            resolved = detect_ats(nav_url)
            if resolved and resolved != "aggregator (unresolved)":
                job_ats = resolved
                break
        if not job_ats:
            job_ats = detect_ats(job.get("application_url") or job.get("url"))

        if job_ats and job_ats != job.get("ats"):
            from applypilot.database import get_connection
            conn = get_connection()
            conn.execute("UPDATE jobs SET ats = ? WHERE url = ?", (job_ats, job["url"]))
            conn.commit()

        # Persist any self-reported widget fixes -- only from a trusted model,
        # and only when the run actually succeeded (a QUIRK line attached to a
        # failed run is an unverified guess, not a confirmed fix). Found via
        # regex rather than a strict line-start match, so a marker glued to
        # the end of the prior sentence (no newline between them) still gets
        # caught -- see the goose.py backend for a case where that happened.
        if "RESULT:APPLIED" in output and model in _TRUSTED_QUIRK_WRITERS and job_ats:
            for m in re.finditer(r"QUIRK:\s*(.+?)(?:\n|$)", output):
                config.append_known_quirk(job_ats, m.group(1).strip())

        # Failure modes carry a much lower bar than quirks (see
        # append_known_issue) -- any model's report is trusted, not just the
        # trusted quirk writers, since a wrong "watch out for X" only wastes a
        # little of the next run's attention.
        if "RESULT:APPLIED" not in output and job_ats:
            for m in re.finditer(r"ISSUE:\s*(.+?)(?:\n|$)", output):
                config.append_known_issue(job_ats, m.group(1).strip())

        if stats:
            with _claude_lock:
                _claude_stats[worker_id] = {
                    "llm_requests": stats.get("turns") or None,
                    "input_tokens": stats.get("input_tokens"),
                    "output_tokens": stats.get("output_tokens"),
                    "cache_read_tokens": stats.get("cache_read"),
                    "cost_usd": stats.get("cost_usd"),
                }
            accumulate_usage(worker_id, stats.get("cost_usd", 0), stats)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        if cdp_dead.is_set():
            add_event(f"[W{worker_id}] BROWSER DIED ({elapsed}s)")
            update_state(worker_id, status="failed",
                         last_action=f"browser died ({elapsed}s)")
            return "failed:page_error", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        try:
            cdp_dead.set()
        except NameError:
            pass  # failed before the watchdog started
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)




# ---------------------------------------------------------------------------
# Backend adapter
# ---------------------------------------------------------------------------

class ClaudeCodeBackend:
    """ApplyBackend implementation backed by the Claude Code CLI."""

    name = "claude"

    def run(self, job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
        return run_job(job, port=port, worker_id=worker_id,
                       model=model, dry_run=dry_run)

    def pop_run_stats(self, worker_id: int) -> dict:
        """Return and clear token accounting from this worker's last run."""
        with _claude_lock:
            return _claude_stats.pop(worker_id, {})

    def preflight(self) -> None:
        """Confirm the Claude Code CLI is on PATH."""
        import shutil
        if not shutil.which("claude"):
            raise RuntimeError(
                "The 'claude' CLI is not on PATH. Install Claude Code from "
                "https://claude.ai/code, or use --backend goose."
            )

    def interrupt_all(self) -> None:
        """Kill every in-flight Claude process (Ctrl+C skip handling)."""
        with _claude_lock:
            procs = list(_claude_procs.items())
        for _wid, proc in procs:
            if proc.poll() is None:
                _kill_process_tree(proc.pid)
