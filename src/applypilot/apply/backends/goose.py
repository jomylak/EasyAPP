"""Goose apply backend -- the default.

Spawns a ``goose run`` session per job with Playwright MCP pointed at the
worker's Chrome (via CDP) and Gmail MCP for verification codes, exactly as the
Claude Code backend does. The agent drives the whole application and prints a
``RESULT:`` line, which is scraped back out of the streamed output.

The difference from the Claude backend is only *which model* is behind the
loop: Goose runs on an OpenRouter model (default ``xiaomi/mimo-v2.5``), so
applications cost cents instead of Claude subscription quota. Everything else
-- the prompt, the known-quirks cache, the ATS resolution, the ``RESULT:``
vocabulary, the dashboard wiring -- is shared with the Claude path.

Grew out of ``scripts/goose_quicktest.sh``, which proved the approach against
real ATS forms before it was promoted to a real backend.

Output parsing
--------------
``goose run --output-format stream-json`` emits newline-delimited JSON:

- ``{"type": "message", "message": {"content": [...]}}`` where each content
  block is ``thinking`` (ignored), ``text`` (streamed one token per envelope,
  so it must be accumulated and scanned at the end), ``toolRequest``, or
  ``toolResponse``.
- ``{"type": "complete", "input_tokens": ..., "output_tokens": ...,
  "cache_read_input_tokens": ..., "cost_usd": ...}`` once at the end.

Requires ``goose`` on PATH and ``OPENROUTER_API_KEY`` in ``~/.applypilot/.env``.
"""

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.ats import detect_ats
from applypilot.apply.chrome import reset_worker_dir, _kill_process_tree
from applypilot.apply.dashboard import add_event, get_state, update_state

logger = logging.getLogger(__name__)

# Track active Goose processes for skip (Ctrl+C) handling
_goose_procs: dict[int, subprocess.Popen] = {}
_goose_stats: dict[int, dict] = {}  # worker_id -> last run's token accounting
_goose_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

def _extension_args(cdp_port: int) -> list[str]:
    """Playwright + Gmail MCP servers, as Goose ``--with-extension`` specs.

    Same two servers the Claude backend declares in its MCP config file, but
    Goose takes them as inline command strings rather than a JSON file. The
    ``name:`` prefix is what makes the tools show up as ``playwright__*`` /
    ``gmail__*`` instead of both being named after ``npx``.
    """
    viewport = config.DEFAULTS["viewport"]
    out_dir = config.playwright_output_dir()
    max_bytes = config.DEFAULTS["playwright_output_max_bytes"]
    return [
        "--with-extension",
        f"playwright:npx @playwright/mcp@latest "
        f"--cdp-endpoint=http://localhost:{cdp_port} --viewport-size={viewport} "
        f"--output-dir={out_dir} --output-max-size={max_bytes}",
        "--with-extension",
        "gmail:npx -y @gongrzhe/server-gmail-autoauth-mcp",
    ]


def _build_command(cdp_port: int, model: str, provider: str, settings: dict | None = None) -> list[str]:
    """Assemble the full ``goose run`` argv."""
    settings = settings or {}
    return [
        "goose", "run",
        "--no-session",          # no session file; every job starts clean
        "--no-profile",          # only the extensions declared here
        "-i", "-",               # prompt on stdin
        "--provider", provider,
        "--model", model,
        "--output-format", "stream-json",
        # A cheap model that loses the thread will otherwise call the same tool
        # forever. Observed healthy runs top out around 140 turns. Settings
        # override lets this be tuned from the Settings tab without a code
        # change; falls back to the measured-good default.
        "--max-turns", str(settings.get("goose_max_turns") or config.DEFAULTS["goose_max_turns"]),
        "--max-tool-repetitions",
        str(settings.get("goose_max_tool_repetitions") or config.DEFAULTS["goose_max_tool_repetitions"]),
        *_extension_args(cdp_port),
    ]


def _strip_extension_prefix(name: str) -> str:
    """``playwright__browser_navigate`` -> ``browser_navigate``.

    Goose namespaces every tool with its extension name, the way Claude Code
    prefixes ``mcp__playwright__``. Stripping it keeps the tool vocabulary
    identical across both backends -- which matters beyond cosmetics, since
    the ATS resolution below matches on the bare ``browser_navigate``.
    """
    return name.split("__", 1)[1] if "__" in name else name


def _describe_tool(name: str, args: dict, extension: str) -> str:
    """One-line dashboard description of a tool call (mirrors the Claude path)."""
    name = _strip_extension_prefix(name)
    label = f"gmail:{name}" if extension == "gmail" else name
    if "url" in args:
        return f"{label} {str(args['url'])[:60]}"
    if "ref" in args:
        return f"{label} {args.get('element', args.get('text', ''))}"[:50]
    if "fields" in args:
        return f"{label} ({len(args['fields'])} fields)"
    if "paths" in args:
        return f"{label} upload"
    return label


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Drive one job application through a Goose session.

    Args:
        job: Job dict from the database.
        port: CDP port of this worker's Chrome.
        worker_id: Numeric worker identifier.
        model: Ignored -- this backend's model is ``goose_model`` in
            settings.json, since a Claude model name means nothing to
            OpenRouter. Accepted only to satisfy the ApplyBackend protocol.
        dry_run: Don't click the final Submit.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    settings = config.load_settings()
    goose_model = settings.get("goose_model") or config.DEFAULTS["goose_model"]
    goose_provider = settings.get("goose_provider") or config.DEFAULTS["goose_provider"]

    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Identical prompt to the Claude path -- same profile, same eligibility
    # rules, same known-quirks cache for this job's ATS.
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    cmd = _build_command(port, goose_model, goose_provider, settings)

    env = os.environ.copy()
    # Goose reads the key from the environment; ~/.applypilot/.env is already
    # loaded into os.environ by _bootstrap(), so this just makes the
    # requirement explicit and fails loudly rather than at the provider.
    if not env.get("OPENROUTER_API_KEY", "").strip() and goose_provider == "openrouter":
        return "failed:no_openrouter_key", 0
    # Don't let an inherited Claude Code session identity leak into the child.
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
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"Backend: goose ({goose_provider} {goose_model})\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    tool_calls = 0
    proc = None
    # Three distinct signals. `done` stops the watchdogs; `cdp_dead` and
    # `timed_out` are diagnoses, and are only ever set by their own watcher --
    # reusing one of them as the stop signal would make its own check below
    # true for every clean run.
    done = threading.Event()
    cdp_dead = threading.Event()
    timed_out = threading.Event()

    try:
        # New process group on Unix so _kill_process_tree (os.killpg) tears
        # down goose and its MCP-server children without also killing
        # whatever process spawned this one -- goose was inheriting our own
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
        with _goose_lock:
            _goose_procs[worker_id] = proc

        # Watchdog: if Chrome's DevTools port dies, the agent has no browser and
        # cannot recover -- but it does not know that, so it sits waiting until
        # the whole run times out. Kill the session promptly instead.
        def _watch_cdp() -> None:
            import socket
            misses = 0
            while not done.wait(10):
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
                                    name=f"goose-cdp-watch-{worker_id}", daemon=True)
        watchdog.start()

        # Wall-clock cap. Unlike the Claude path -- where the streamed output
        # ends when the CLI decides it is done -- a cheap model can keep the
        # loop alive well past the point of being useful, and --max-turns only
        # bounds turns, not time.
        def _watch_clock() -> None:
            limit = settings.get("goose_timeout") or config.DEFAULTS["goose_timeout"]
            if done.wait(limit):
                return  # run finished (or died) before the cap
            if proc.poll() is None:
                logger.error("[worker-%d] Goose run exceeded %ds; killing",
                             worker_id, limit)
                timed_out.set()
                _kill_process_tree(proc.pid)

        clock = threading.Thread(target=_watch_clock,
                                 name=f"goose-clock-{worker_id}", daemon=True)
        clock.start()

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
                except json.JSONDecodeError:
                    # Goose prints a small ASCII banner before the stream.
                    lf.write(line + "\n")
                    continue

                msg_type = msg.get("type")

                if msg_type == "complete":
                    stats = {
                        "input_tokens": msg.get("input_tokens", 0),
                        "output_tokens": msg.get("output_tokens", 0),
                        "cache_read": msg.get("cache_read_input_tokens", 0),
                        "cost_usd": msg.get("cost_usd", 0),
                    }
                    continue

                if msg_type != "message":
                    continue

                for block in msg.get("message", {}).get("content", []) or []:
                    bt = block.get("type")
                    if bt == "text":
                        # Streamed one token per envelope; join at the end.
                        text_parts.append(block.get("text", ""))
                    elif bt == "toolRequest":
                        call = (block.get("toolCall") or {}).get("value") or {}
                        name = call.get("name", "")
                        args = call.get("arguments") or {}
                        extension = (block.get("_meta") or {}).get("goose_extension", "")
                        if _strip_extension_prefix(name) == "browser_navigate" and "url" in args:
                            navigated_urls.append(args["url"])
                        desc = _describe_tool(name, args, extension)
                        tool_calls += 1
                        lf.write(f"  >> {desc}\n")
                        update_state(worker_id, actions=tool_calls,
                                     last_action=desc[:35])

        proc.wait(timeout=30)
        returncode = proc.returncode
        proc = None
        done.set()  # stop both watchdogs; leaves their diagnoses intact

        if returncode and returncode < 0 and not timed_out.is_set() and not cdp_dead.is_set():
            return "skipped", int((time.time() - start) * 1000)

        output = "".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"goose_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        # Resolve the real ATS platform for this run. The stored URL is often
        # an aggregator redirect (Jobright, Intern List) that detect_ats can't
        # resolve -- but the agent's own browser_navigate calls reveal the real
        # destination once it follows the posting to the employer's ATS.
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

        # Persist self-reported widget fixes, only from a run that actually
        # succeeded -- a QUIRK line on a failed run is an unverified guess.
        # See `goose_writes_quirks` in config for why this is a setting.
        #
        # Found via regex, not line-by-line startswith: `output` is built by
        # "".join(text_parts) across separate goose messages, so a marker that
        # opens its own message lands glued to the end of the prior message's
        # last sentence with no newline between them -- a plain
        # `.strip().startswith("QUIRK:")` silently misses it whenever that
        # happens (confirmed missing a real ISSUE: line this way).
        if ("RESULT:APPLIED" in output and job_ats
                and config.load_settings().get(
                    "goose_writes_quirks", config.DEFAULT_SETTINGS["goose_writes_quirks"])):
            for m in re.finditer(r"QUIRK:\s*(.+?)(?:\n|$)", output):
                config.append_known_quirk(job_ats, m.group(1).strip())

        # Persist self-reported failure modes -- these carry a much lower bar
        # than quirks (see append_known_issue): a wrong "watch out for X" only
        # wastes a little of the next run's attention, so any model's report
        # is trusted, not just the trusted quirk writers.
        if "RESULT:APPLIED" not in output and job_ats:
            for m in re.finditer(r"ISSUE:\s*(.+?)(?:\n|$)", output):
                config.append_known_issue(job_ats, m.group(1).strip())

        if stats:
            with _goose_lock:
                _goose_stats[worker_id] = {
                    # Each tool result triggers a fresh inference, so tool
                    # calls + the opening turn is the request count. Goose
                    # reports no turn counter of its own in stream-json.
                    "llm_requests": tool_calls + 1,
                    "input_tokens": stats.get("input_tokens"),
                    "output_tokens": stats.get("output_tokens"),
                    "cache_read_tokens": stats.get("cache_read"),
                    "cost_usd": stats.get("cost_usd"),
                }
            cost = stats.get("cost_usd", 0) or 0
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

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

        if timed_out.is_set():
            add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
            return "failed:timeout", duration_ms

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
        done.set()
        with _goose_lock:
            _goose_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Backend adapter
# ---------------------------------------------------------------------------

class GooseBackend:
    """ApplyBackend implementation backed by the Goose CLI on OpenRouter."""

    name = "goose"

    def run(self, job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
        return run_job(job, port=port, worker_id=worker_id,
                       model=model, dry_run=dry_run)

    def pop_run_stats(self, worker_id: int) -> dict:
        """Return and clear token accounting from this worker's last run."""
        with _goose_lock:
            return _goose_stats.pop(worker_id, {})

    def preflight(self) -> None:
        """Confirm the Goose CLI is on PATH and the provider key is set."""
        if not shutil.which("goose"):
            raise RuntimeError(
                "The 'goose' CLI is not on PATH. Install it from "
                "https://block.github.io/goose/docs/getting-started/installation/ "
                "(then make sure ~/.local/bin is on PATH), or run with "
                "--backend claude."
            )
        settings = config.load_settings()
        provider = settings.get("goose_provider") or config.DEFAULTS["goose_provider"]
        if provider == "openrouter" and not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise RuntimeError(
                f"OPENROUTER_API_KEY is not set. Add it to {config.ENV_PATH} "
                "(get a key at https://openrouter.ai/keys), or run with "
                "--backend claude."
            )

    def interrupt_all(self) -> None:
        """Kill every in-flight Goose process (Ctrl+C skip handling)."""
        with _goose_lock:
            procs = list(_goose_procs.items())
        for _wid, proc in procs:
            if proc.poll() is None:
                _kill_process_tree(proc.pid)
