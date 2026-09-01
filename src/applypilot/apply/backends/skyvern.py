"""Skyvern apply backend.

Drives one job application through a local Skyvern server instead of a Claude
Code session, so the work runs on whatever model Skyvern is configured with
(typically a cheap OpenRouter one) rather than against Claude subscription
quota.

Three things make this fit ApplyPilot rather than replace it:

- **Same browser.** Skyvern attaches over CDP to the Chrome that ``chrome.py``
  already launched for this worker, so it inherits the cloned profile and its
  ATS session cookies. It does not open its own browser.
- **Same documents.** The tailored resume is served over loopback by
  ``fileserver`` and handed to Skyvern as a URL, because Skyvern uploads files
  by downloading them first. Nothing is published off the machine.
- **Same outcome vocabulary.** ``outcomes.ERROR_CODE_MAPPING`` is passed as
  Skyvern's ``error_code_mapping`` and the run reports through a
  ``data_extraction_schema``, so results land in the database with exactly the
  reason codes the Claude Code path produces -- no lossy remapping.

Requires the optional dependency: ``pip install "applypilot[skyvern]"`` and a
running server (``skyvern run server``).
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime

from applypilot import config
from applypilot.apply import outcomes
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.dashboard import add_event, update_state
from applypilot.apply.fileserver import DocumentServer
from applypilot.apply.verification import VerificationServer, VerificationService

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "The Skyvern backend needs the optional 'skyvern' dependency.\n"
    "  pip install \"applypilot[skyvern]\"   (or: pip install skyvern)\n"
    "Then start the server in another terminal:  skyvern run server"
)

# The run reports its outcome through this schema rather than by printing a
# line we regex out of prose, which is how the Claude Code path does it.
RESULT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "enum": ["applied", "expired", "captcha", "login_issue", "failed"],
            "description": "Use 'applied' only if the application was actually submitted and confirmed.",
        },
        "reason": {
            "type": "string",
            "description": (
                "Required when result is 'failed'. One of: "
                + ", ".join(sorted(outcomes.ERROR_CODE_MAPPING)) + "."
            ),
        },
        "notes": {
            "type": "string",
            "description": "One short sentence on what happened, for a human reading the log.",
        },
    },
    "required": ["result"],
}


def _coerce_output(output) -> dict:
    """Normalise Skyvern's ``output`` field into a dict.

    It is typed ``dict | list | str | None``, and models sometimes nest the
    payload or return it as a JSON string, so unwrap defensively rather than
    assuming the happy shape.
    """
    if output is None:
        return {}
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except (ValueError, TypeError):
            return {"result": "failed", "reason": "unknown", "notes": output[:200]}
    if isinstance(output, list):
        output = next((o for o in output if isinstance(o, dict)), {})
    if not isinstance(output, dict):
        return {}
    # Some engines wrap the extraction under a key rather than returning it flat.
    if "result" not in output:
        for key in ("extracted_information", "extracted_data", "data", "output"):
            nested = output.get(key)
            if isinstance(nested, dict) and "result" in nested:
                return nested
    return output


def _normalise_reason(text: str | None) -> str:
    """Map Skyvern's free-text failure_reason onto a known reason code.

    Falls back to a truncated slug so an unrecognised failure still lands in
    the database as something greppable rather than an empty string.
    """
    if not text:
        return "unknown"
    lowered = text.lower()
    for code in outcomes.ERROR_CODE_MAPPING:
        if code in lowered:
            return code
    # Common Skyvern phrasings that don't literally contain a reason code.
    if "max steps" in lowered or "step limit" in lowered:
        return "stuck"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    slug = "".join(c if c.isalnum() else "_" for c in lowered.strip())[:60]
    return slug.strip("_") or "unknown"


def _map_outcome(status: str, output, failure_reason: str | None) -> str:
    """Translate a finished Skyvern run into a launcher status string."""
    status = str(status or "").lower()

    if status == "completed":
        data = _coerce_output(output)
        result = str(data.get("result") or "").lower().strip()
        if result == "applied":
            return "applied"
        if result in ("expired", "captcha", "login_issue"):
            return result
        reason = _normalise_reason(str(data.get("reason") or "") or data.get("notes"))
        return f"failed:{reason}"

    if status == "timed_out":
        return "failed:timeout"
    if status == "canceled":
        return "skipped"

    # failed / terminated -- Skyvern gave up rather than the model reporting.
    return f"failed:{_normalise_reason(failure_reason)}"


class SkyvernBackend:
    """ApplyBackend implementation backed by a local Skyvern server."""

    name = "skyvern"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: set[int] = set()  # worker_ids with a run outstanding
        self._stats: dict[int, dict] = {}  # worker_id -> last run's telemetry

    # -- configuration ----------------------------------------------------

    @staticmethod
    def _client():
        """Construct the Skyvern SDK client, with actionable errors."""
        try:
            from skyvern import Skyvern
        except ImportError as exc:
            raise RuntimeError(INSTALL_HINT) from exc

        config.load_env()
        import os

        base_url = (os.environ.get("SKYVERN_BASE_URL")
                    or config.DEFAULTS["skyvern_base_url"] or "").strip()
        # The SDK falls back to Skyvern *Cloud* when base_url is empty, which
        # would ship the candidate's resume and profile off the machine. Refuse
        # rather than let a blank env var silently change where data goes.
        if not base_url:
            raise RuntimeError(
                "SKYVERN_BASE_URL is empty. Set it to your local server "
                "(e.g. http://localhost:8000); leaving it blank would send this "
                "job to Skyvern Cloud."
            )
        api_key = os.environ.get("SKYVERN_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "SKYVERN_API_KEY is not set. Start the server with 'skyvern run server' "
                "and copy the API key it prints (or the one in Skyvern's .env) into "
                f"{config.ENV_PATH}."
            )
        return Skyvern(api_key=api_key, base_url=base_url)

    def preflight(self) -> None:
        """Check the dependency, the API key, and that the server is actually up.

        A server that isn't running is by far the most likely failure, and it
        is much cheaper to say so now than after Chrome has launched and a job
        has been locked as in_progress.
        """
        self._client()  # raises with an install/API-key hint if unusable

        import os
        import urllib.error
        import urllib.request

        config.load_env()
        base_url = (os.environ.get("SKYVERN_BASE_URL")
                    or config.DEFAULTS["skyvern_base_url"]).rstrip("/")
        try:
            urllib.request.urlopen(f"{base_url}/heartbeat", timeout=5)
        except urllib.error.HTTPError:
            # Reachable but that path may not exist on this version -- the
            # server is up, which is all we are checking for.
            pass
        except Exception as exc:
            raise RuntimeError(
                f"No Skyvern server reachable at {base_url} ({exc}).\n"
                "Start it in another terminal with:  skyvern run server\n"
                "Or set SKYVERN_BASE_URL if it listens elsewhere."
            ) from exc

    # -- execution --------------------------------------------------------

    def run(self, job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
        """Apply to one job via Skyvern. ``model`` is ignored -- Skyvern's model
        is configured server-side in its own ``.env``."""
        start = time.time()
        try:
            status = asyncio.run(self._run_async(job, port, worker_id, dry_run))
        except RuntimeError as exc:
            # Configuration/dependency problems -- surface them loudly, they are
            # not the job's fault and every subsequent job will hit them too.
            logger.error("[worker-%d] Skyvern backend unavailable: %s", worker_id, exc)
            add_event(f"[W{worker_id}] Skyvern unavailable: {str(exc)[:40]}")
            update_state(worker_id, status="failed", last_action="skyvern unavailable")
            return f"failed:skyvern_unavailable", int((time.time() - start) * 1000)
        except Exception as exc:
            logger.exception("[worker-%d] Skyvern run crashed", worker_id)
            add_event(f"[W{worker_id}] ERROR: {str(exc)[:40]}")
            update_state(worker_id, status="failed", last_action=f"ERROR: {str(exc)[:25]}")
            return f"failed:{str(exc)[:100]}", int((time.time() - start) * 1000)

        return status, int((time.time() - start) * 1000)

    async def _run_async(self, job: dict, port: int, worker_id: int, dry_run: bool) -> str:
        ctx = prompt_mod._prepare_context(job, worker_id=worker_id)

        resume_path = job.get("tailored_resume_path")
        resume_text = ""
        if resume_path:
            from pathlib import Path
            txt = Path(resume_path).with_suffix(".txt")
            if txt.exists():
                resume_text = txt.read_text(encoding="utf-8")

        apply_url = job.get("application_url") or job["url"]
        elapsed_start = time.time()

        update_state(worker_id, status="applying", job_title=job["title"],
                     company=job.get("site", ""), score=job.get("fit_score", 0),
                     start_time=elapsed_start, actions=0, last_action="starting skyvern")
        add_event(f"[W{worker_id}] Skyvern: {job['title'][:40]} @ {job.get('site', '')}")

        worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
        header = (
            f"\n{'=' * 60}\n"
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] SKYVERN "
            f"{job['title']} @ {job.get('site', '')}\n"
            f"URL: {apply_url}\n{'=' * 60}\n"
        )

        # Skyvern uploads files by downloading them, so the resume has to be
        # reachable over HTTP. Loopback only -- see fileserver's module docs.
        with DocumentServer(ctx["dest_dir"], worker_id=worker_id,
                            port_base=config.DEFAULTS["skyvern_file_port_base"]) as docs:
            resume_url = docs.url_for(f"{ctx['full_name'].replace(' ', '_')}_Resume.pdf")
            cl_url = ""
            if ctx["cl_upload_path"]:
                from pathlib import Path
                cl_url = docs.url_for(Path(ctx["cl_upload_path"]).name)

            goal = prompt_mod.build_skyvern_goal(
                job=job,
                tailored_resume=resume_text,
                resume_url=resume_url,
                cover_letter_url=cl_url,
                cover_letter_text=ctx["cover_letter_text"],
                profile_summary=ctx["profile_summary"],
                location_check=ctx["location_check"],
                salary_section=ctx["salary_section"],
                screening_section=ctx["screening_section"],
                hard_rules=ctx["hard_rules"],
                display_name=ctx["display_name"],
                phone_digits=ctx["phone_digits"],
                personal=ctx["personal"],
                dry_run=dry_run,
            )

            client = self._client()

            # Email verification. Two mechanisms, because employers use both:
            # Skyvern POSTs to totp_url when a form asks for a code, and a
            # background watcher opens one-time login *links* in this same
            # Chrome -- there is nowhere to type a link, and Skyvern's own
            # answer to magic links is to split the run in two.
            verifier = VerificationService(
                cdp_port=port, started_at=elapsed_start, lookback_minutes=15,
            )
            totp_server = VerificationServer(
                verifier, worker_id=worker_id,
                port_base=config.DEFAULTS["skyvern_totp_port_base"],
            )
            stop_watch = threading.Event()

            def _watch_for_links() -> None:
                interval = config.DEFAULTS["verification_link_poll_seconds"]
                while not stop_watch.wait(interval):
                    try:
                        verifier.check_for_links()
                    except Exception:
                        logger.debug("Link watcher iteration failed", exc_info=True)

            watcher = threading.Thread(
                target=_watch_for_links, name=f"verify-{worker_id}", daemon=True,
            )

            update_state(worker_id, last_action="skyvern running")

            with self._lock:
                self._in_flight.add(worker_id)
            try:
                totp_server.start()
                watcher.start()
                run = await client.run_task(
                    prompt=goal,
                    url=apply_url,
                    # Attach to THIS worker's Chrome, with its logged-in profile.
                    browser_address=f"http://127.0.0.1:{port}",
                    data_extraction_schema=RESULT_SCHEMA,
                    error_code_mapping=outcomes.ERROR_CODE_MAPPING,
                    max_steps=config.DEFAULTS["skyvern_max_steps"],
                    # Where Skyvern fetches an emailed verification code.
                    totp_url=totp_server.url,
                    totp_identifier=ctx["personal"].get("email") or None,
                    wait_for_completion=True,
                    timeout=config.DEFAULTS["skyvern_timeout"],
                )
            finally:
                stop_watch.set()
                watcher.join(timeout=3)
                totp_server.stop()
                with self._lock:
                    self._in_flight.discard(worker_id)

        run_id = getattr(run, "run_id", None)
        status = getattr(run, "status", None)
        output = getattr(run, "output", None)
        failure_reason = getattr(run, "failure_reason", None)
        recording = getattr(run, "recording_url", None)
        app_url = getattr(run, "app_url", None)

        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(header)
            lf.write(f"  run_id: {run_id}\n  status: {status}\n")
            if failure_reason:
                lf.write(f"  failure_reason: {failure_reason}\n")
            if output:
                lf.write(f"  output: {output}\n")
            if recording:
                lf.write(f"  recording: {recording}\n")
            if app_url:
                lf.write(f"  run in UI: {app_url}\n")

        step_count = getattr(run, "step_count", None)
        if step_count is not None:
            with self._lock:
                self._stats[worker_id] = {"llm_requests": step_count}

        result = _map_outcome(str(status), output, failure_reason)
        elapsed = int(time.time() - elapsed_start)

        label = result.split(":", 1)[0].upper() if ":" in result else result.upper()
        detail = result.split(":", 1)[1] if ":" in result else ""
        add_event(f"[W{worker_id}] {label}{'/' + detail[:20] if detail else ''} ({elapsed}s)")
        update_state(worker_id,
                     status=result.split(":", 1)[0],
                     last_action=f"{label} ({elapsed}s)")
        return result

    def pop_run_stats(self, worker_id: int) -> dict:
        """Return and clear the step count from this worker's last run."""
        with self._lock:
            return self._stats.pop(worker_id, {})

    # -- interruption -----------------------------------------------------

    def interrupt_all(self) -> None:
        """No-op by design -- Chrome teardown is what actually stops a run.

        ``run_task`` is called with ``wait_for_completion=True``, so it blocks
        until the run finishes and never yields a run id we could cancel
        mid-flight. There is no cancel method on the SDK's high-level surface
        either. What genuinely ends a Skyvern run is the worker loop killing
        this worker's Chrome (``cleanup_worker``): the run loses its browser
        and terminates on its own, which the launcher then records.

        Kept explicit rather than silently absent so this stays honest about
        what Ctrl+C does here, and so the launcher can call it uniformly.
        """
        with self._lock:
            outstanding = sorted(self._in_flight)
        if outstanding:
            logger.info(
                "Skyvern runs outstanding on workers %s; they end when Chrome is torn down",
                outstanding,
            )
