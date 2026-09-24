"""Shared fixture: a live `applypilot serve` on a throwaway, seeded APPLYPILOT_DIR.

Never touches ~/.applypilot -- the server subprocess only ever sees the temp dir.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

SEED = """
import random
from datetime import datetime, timedelta, timezone
from applypilot.database import get_connection, init_db
init_db()
conn = get_connection()
now = datetime.now(timezone.utc)
rng = random.Random(0)
for i in range(60):
    day = (now - timedelta(days=i % 4, hours=1)).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, location, discovered_at, posted_date, "
        "job_type, term, fit_score, desirability_score, company_prestige, eligible, "
        "full_description, pay_text, pay_min_hourly, pay_max_hourly, scored_at, ats) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"https://example.com/job/{i}", f"Software Engineer Intern {i}", f"Company {i % 7}",
         "greenhouse", ["New York, NY", "Remote", "San Francisco, CA"][i % 3], day, day,
         ["internship", "new_grad"][i % 2], "summer", rng.randint(1, 10), rng.uniform(1, 10),
         rng.randint(1, 10), "yes", "Build things. " * 20, "$40-$60/hr", 40.0, 60.0, day, "greenhouse"),
    )
conn.commit()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    app_dir = tmp_path_factory.mktemp("applypilot")
    env = {**os.environ, "APPLYPILOT_DIR": str(app_dir)}
    env.pop("APPLYPILOT_HOST", None)
    subprocess.run([sys.executable, "-c", SEED], env=env, check=True)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "applypilot.cli", "serve", "--no-open", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/api/stats", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server never came up:\n" + proc.stdout.read().decode())
    yield url
    proc.terminate()
    proc.wait(timeout=10)
