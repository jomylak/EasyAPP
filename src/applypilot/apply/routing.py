"""ATS-aware job routing across workers that share proxy IPs.

Workers are grouped by the static proxy they launch through (APPLY_PROXY_<n>),
so any number of IPs and any number of workers per IP works with no config
here -- two workers pointed at the same host:port are one group, a worker with
no proxy is the "direct" group.

acquire_job hands pick() a small window of the best-ranked candidates (already
filtered for company lock/cap) and pick() chooses among them. It is a
preference, never a gate: something is always returned if the window is
non-empty, so a lopsided backlog (all Workday) still keeps every worker busy.
Order of preference:
  1. an ATS no other worker on this IP is running right now
  2. an ATS this IP has claimed least so far this run (spreads each ATS
     evenly across IPs)
  3. original queue rank
The company lock itself lives in launcher._company_locked_reason and is global
across IPs, which is strictly stronger than per-IP.
"""

import threading
from collections import Counter

from applypilot import config
from applypilot.ats import detect_ats

# Candidates considered per claim. Bigger = more ATS spread but a top-ranked
# job can wait behind more lower-ranked ones.
ROUTING_WINDOW = 10

_claims: Counter = Counter()  # (ip_key, ats) -> jobs claimed this process
_lock = threading.Lock()


def ip_key(worker_id: int) -> str:
    proxy = config.get_worker_proxy_config(worker_id)
    return f"{proxy['host']}:{proxy['port']}" if proxy else "direct"


def _ats(row) -> str:
    return detect_ats(row["application_url"] or row["url"]) or "unknown"


def pick(conn, rows, worker_id: int):
    """Best row from `rows` (queue-ranked) for this worker, or None if empty."""
    if not rows:
        return None
    ip = ip_key(worker_id)
    me = f"worker-{worker_id}"
    busy = Counter(
        _ats(r) for r in conn.execute(
            "SELECT agent_id, application_url, url FROM jobs "
            "WHERE apply_status = 'in_progress' AND agent_id LIKE 'worker-%'")
        if r["agent_id"] != me and ip_key(int(r["agent_id"][7:])) == ip)
    with _lock:
        def score(i_row):
            i, row = i_row
            ats = _ats(row)
            return (busy[ats], _claims[(ip, ats)], i)
        return min(enumerate(rows), key=score)[1]


def record_claim(worker_id: int, row) -> None:
    with _lock:
        _claims[(ip_key(worker_id), _ats(row))] += 1
