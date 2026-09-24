"""Keep the Webshare proxy pool at a good fraud score by swapping bad IPs.

Two triggers: ensure() at the start of every apply run, and on_block() when a
job hits a captcha/block on its proxy. Either way an IP that is non-US, over
the fraud limit (IPQS, with AbuseIPDB as second gate/fallback -- see check())
is replaced with a US IP, repeatedly, until a good one lands or the per-slot
try limit / monthly swap quota runs out. It then writes
APPLY_PROXY_0..SLOTS-1 into ~/.applypilot/.env, spread round-robin over the
pool (4 proxies -> 2 workers each). The home relay (APPLY_PROXY) is untouched.

The monthly quota is counted locally (Webshare's API gives no remaining
count that I could confirm) and shown on the dashboard via state().
"""
import json
import os
import re
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config
from applypilot.apply import ip_health, routing

STATE = Path("logs/proxy_swaps.json")
API = "https://proxy.webshare.io/api"
MAX_FRAUD = int(os.environ.get("PROXY_MAX_FRAUD", 40))
MAX_TRIES = 5  # swaps per slot per check
SLOTS = 8  # APPLY_PROXY_0..7
MAX_ABUSE = int(os.environ.get("PROXY_MAX_ABUSE", 25))  # AbuseIPDB confidence %
# How stale a cached score may be before before_job() re-fetches it. IPQS is
# call-capped (~33/day) so it gets a long TTL; AbuseIPDB (~1000/day) is cheap.
IPQS_TTL = 6 * 3600
ABUSE_TTL = 30 * 60
STRIKES = 3  # blocks on one IP before it's swapped regardless of score
QUOTA = int(os.environ.get("WEBSHARE_MONTHLY_SWAPS", 48))


_lock = threading.RLock()  # one swap at a time; workers are threads of one process
_strikes: Counter = Counter()  # ip -> blocks seen on it since it joined the pool
_ours: set[str] = set()  # pool IPs as of the last write, so before_job needs no API call


def enabled() -> bool:
    config.load_env()
    return bool(os.environ.get("WEBSHARE_API_KEY", "").strip())


def _call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Token {os.environ['WEBSHARE_API_KEY'].strip()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def pool() -> list[dict]:
    d = _call("GET", "/v2/proxy/list/?mode=direct&page_size=100")
    return sorted((p for p in d["results"] if p.get("valid", True)),
                  key=lambda p: p["proxy_address"])


def state() -> dict:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        st = json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    if st.get("month") != month:  # quota resets monthly; history is kept
        st = {"month": month, "remaining": QUOTA, "quota": QUOTA, "swaps": st.get("swaps", [])}
    return st


def _save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st))


def _swap(ip: str) -> list[dict]:
    """Replace one IP and return the new pool once it's gone from the list."""
    _call("POST", "/v3/proxy/replace/", {
        "to_replace": {"type": "ip_address", "ip_addresses": [ip]},
        "replace_with": [{"type": "country", "country_code": "US"}],
        "dry_run": False})
    for _ in range(20):  # replacement is async
        time.sleep(3)
        cur = pool()
        if ip not in {p["proxy_address"] for p in cur}:
            return cur
    raise RuntimeError(f"Webshare did not replace {ip} within 60s")


def _write_env(px: list[dict]) -> None:
    keep = [ln for ln in config.ENV_PATH.read_text().splitlines()
            if not re.match(r"APPLY_PROXY_\d+=", ln)]
    new = {}
    for w in range(SLOTS):
        p = px[w % len(px)]
        new[f"APPLY_PROXY_{w}"] = f"{p['proxy_address']}:{p['port']}:{p['username']}:{p['password']}"
    config.ENV_PATH.write_text("\n".join(keep + [f"{k}={v}" for k, v in new.items()]) + "\n")
    os.environ.update(new)
    _ours.clear()
    _ours.update(p["proxy_address"] for p in px)


def check(ip: str, fresh: bool = False) -> tuple[int | None, bool | None]:
    """(score, bad) for an IP; bad is None when no service could answer.

    IPQS is primary. AbuseIPDB is a second gate (either one over its limit
    marks the IP bad) and the fallback when IPQS is out of daily calls or
    down. fresh=True bypasses the cache, otherwise entries older than
    IPQS_TTL / ABUSE_TTL are re-fetched -- these services lag, so a cached
    "clean" isn't trusted for long.
    """
    q = ip_health.fraud_score(ip, 0 if fresh else IPQS_TTL)
    a = ip_health.abuse_score(ip, 0 if fresh else ABUSE_TTL)
    if q is None and a is None:
        return None, None
    return (q if q is not None else a), (q or 0) > MAX_FRAUD or (a or 0) >= MAX_ABUSE


def _fix_slot(px, ip, st, log, fresh=False, force=False):
    """Swap `ip` until the replacement is a good US IP or limits run out.
    force treats the first look as bad (repeated blocks on a clean-scoring IP).
    Returns the updated pool."""
    pending = None  # the swap whose new_score we're about to learn
    for attempt in range(MAX_TRIES + 1):
        p = next(p for p in px if p["proxy_address"] == ip)
        if p.get("country_code", "US") != "US":
            score, bad = None, True
        else:
            score, bad = check(ip, fresh)
        if pending:
            pending["new_score"] = score
            _save(st)
        if bad is None:
            log(f"{ip}: no score (IPQS/AbuseIPDB unavailable), keeping")
            break
        log(f"{ip}: score {score}{' (forced)' if force and attempt == 0 else ''}")
        if not (bad or (force and attempt == 0)):
            break
        if st["remaining"] <= 0 or attempt == MAX_TRIES:
            log(f"{ip}: still bad, out of {'swaps' if st['remaining'] <= 0 else 'tries'}")
            break
        before = {p["proxy_address"] for p in px}
        px = _swap(ip)
        st["remaining"] -= 1
        fresh_ips = [p["proxy_address"] for p in px if p["proxy_address"] not in before]
        pending = {"ts": datetime.now(timezone.utc).isoformat(), "old_ip": ip,
                   "old_score": score, "new_ip": fresh_ips[0] if fresh_ips else None,
                   "new_score": None}
        st["swaps"] = (st["swaps"] + [pending])[-50:]
        _save(st)
        _strikes.pop(ip, None)
        if not fresh_ips:
            break
        ip = fresh_ips[0]
        fresh = True  # a brand-new IP has no cache anyway; keeps intent clear
    return px


def ensure(log=print) -> list[dict]:
    """Run-start check: every pool IP scored, bad/non-US ones swapped."""
    config.load_env()
    with _lock:
        st, px = state(), pool()
        if not px:
            raise RuntimeError("Webshare pool is empty")
        for ip in [p["proxy_address"] for p in px]:  # one slot per original IP
            px = _fix_slot(px, ip, st, log)
        _write_env(px)
        return px


def on_block(worker_id: int, log=print) -> None:
    """A job on this worker's proxy hit a captcha/block: re-score that IP
    fresh and swap it if bad. Blocks are the live signal the score services
    lag behind, so STRIKES blocks on one IP swap it even if it scores clean.
    No-op for workers not on a Webshare IP (e.g. the home relay)."""
    if not enabled():
        return
    ip = routing.ip_key(worker_id).split(":")[0]
    with _lock:
        px = pool()
        if ip not in {p["proxy_address"] for p in px}:
            return  # not ours, or another worker already swapped it
        _strikes[ip] += 1
        st = state()
        px = _fix_slot(px, ip, st, log, fresh=True, force=_strikes[ip] >= STRIKES)
        _write_env(px)


def before_job(worker_id: int, log=print) -> None:
    """Called before every job: re-validate this worker's IP (cheap -- served
    from cache until the TTLs above expire) and swap it if it has gone bad.
    No-op for workers not on a Webshare IP."""
    if not enabled():
        return
    ip = routing.ip_key(worker_id).split(":")[0]
    if ip not in _ours:
        return
    _, bad = check(ip)
    if not bad:
        return
    with _lock:
        px = pool()
        if ip in {p["proxy_address"] for p in px}:  # else a peer already swapped it
            _write_env(_fix_slot(px, ip, state(), log, fresh=True))
