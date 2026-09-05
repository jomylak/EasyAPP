"""Pluggable apply backends.

A backend takes one acquired job and drives it to a terminal status. Every
backend returns the same ``(status, duration_ms)`` tuple the worker loop has
always expected, so job acquisition, Chrome lifecycle, the dashboard, and the
retry/review classification in ``launcher`` stay backend-agnostic.

- ``goose``  -- the default: a ``goose run`` session driving Playwright MCP
  against the worker's Chrome on a cheap OpenRouter model. Costs cents per
  application and nothing against the Claude quota.
- ``claude`` -- the original path: a ``claude -p`` session driving the *same*
  MCP servers against the same Chrome. Stronger driver, costs Claude
  subscription quota. Used as the fallback when Goose gives up on a job
  (see ``launcher.worker_loop``).

Backends are singletons: each keeps a registry of its in-flight child
processes / runs so Ctrl+C can interrupt them.
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

BACKEND_NAMES = ("goose", "claude")


class ApplyBackend(Protocol):
    """Drives a single job application to a terminal status."""

    name: str

    def run(self, job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
        """Apply to one job.

        Returns:
            Tuple of (status_string, duration_ms). Status is one of
            'applied', 'expired', 'captcha', 'login_issue',
            'failed:reason', or 'skipped'.
        """
        ...

    def interrupt_all(self) -> None:
        """Abort every in-flight run (Ctrl+C handling)."""
        ...

    def pop_run_stats(self, worker_id: int) -> dict:
        """Return and clear telemetry for this worker's last completed run.

        Currently ``{"llm_requests": int}`` where the backend can report it.
        Empty dict when the backend has no such counter.
        """
        ...

    def preflight(self) -> None:
        """Validate dependencies and configuration before any job is locked.

        Constructing a backend must stay cheap and side-effect free, so the
        real readiness check lives here and is called once at startup.

        Raises:
            RuntimeError: With an actionable message if the backend cannot run.
        """
        ...


# Singleton per backend name -- backends hold in-flight run registries, so a
# fresh instance per call would lose track of what needs interrupting.
_instances: dict[str, ApplyBackend] = {}


def get_backend(name: str) -> ApplyBackend:
    """Look up a backend by name, constructing it on first use.

    Args:
        name: Either 'goose' or 'claude'.

    Raises:
        ValueError: If the name is not a known backend.
        RuntimeError: If the backend's optional dependencies are missing.
    """
    name = (name or "goose").lower()
    if name in _instances:
        return _instances[name]

    if name == "goose":
        from applypilot.apply.backends.goose import GooseBackend
        backend: ApplyBackend = GooseBackend()
    elif name == "claude":
        from applypilot.apply.backends.claude_code import ClaudeCodeBackend
        backend = ClaudeCodeBackend()
    else:
        raise ValueError(
            f"Unknown apply backend {name!r}. Expected one of: {', '.join(BACKEND_NAMES)}"
        )

    _instances[name] = backend
    return backend


def interrupt_all_backends() -> None:
    """Interrupt in-flight runs on every backend that has been used."""
    for backend in list(_instances.values()):
        try:
            backend.interrupt_all()
        except Exception:
            logger.debug("Backend %s failed to interrupt cleanly", backend.name, exc_info=True)
