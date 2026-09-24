"""Schemathesis fuzz of the web API: thousands of generated requests, fail on any 5xx.

Endpoints that start real work (apply runs, Gmail, credential checks, login
sessions) and the never-ending /api/events stream are excluded.
"""

import shutil
import subprocess

import pytest

EXCLUDE = r"^/api/(events|launch|stop|stop-all|gmail-scan|credential-check|login-session/.*)$"


@pytest.mark.skipif(not shutil.which("schemathesis"), reason="pip install schemathesis")
def test_api_never_500s(server):
    r = subprocess.run(
        ["schemathesis", "run", f"{server}/openapi.json",
         "--exclude-path-regex", EXCLUDE, "--checks", "not_a_server_error",
         "--request-timeout", "10", "--max-examples", "200"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout[-6000:]
