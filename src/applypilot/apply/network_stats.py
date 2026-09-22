"""Reads the free network-usage telemetry mcp_tools/server.py collects.

The actual polling (performance.getEntriesByType, real transferred bytes)
happens inside the applytools MCP server process, on the same CDP
connection it already uses for the dry-run guard and every other tool --
see server.py's _netstats_poll_loop docstring for why that's the reliable
path (a separate connect_over_cdp() observer process here first, watching
the same Chrome, got TargetClosedError on every poll and saw nothing).

This module is just launcher.py's read side: reset the file before a job
starts (so a new worker job doesn't inherit the previous job's numbers on
the same CDP port), then read it back after the backend run finishes.
"""

import json
from pathlib import Path


def _path(port: int) -> Path:
    return Path(f"/tmp/applypilot_netstats_{port}.json")


def reset(port: int) -> None:
    _path(port).unlink(missing_ok=True)


def read(port: int) -> dict:
    """Best-effort: {} if the applytools extension never got called this
    run (rare -- only happens if the agent used nothing but raw browser_*
    tools) or the file otherwise never appeared.
    """
    try:
        return json.loads(_path(port).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
