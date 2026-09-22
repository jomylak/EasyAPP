"""Per-job exit-IP health telemetry: who the IP was, how risky it looked, how the job went.

One JSONL line per apply run in logs/ip_health.jsonl (same no-migration
pattern as network_stats.jsonl). Two lookups, both best-effort -- a failure
here must never block or fail a job:

* ip-api.com through the worker's own proxy (~1KB of proxy bandwidth): the
  exit IP itself plus ISP/ASN/city/country and its hosting/proxy flags. Done
  per Chrome launch, not cached by proxy host:port like geo_fingerprint, since
  a rotating provider's sticky session gets a different IP every launch.
* IPQualityScore fraud score, called directly (not through the proxy, so it
  costs no proxy GB) and cached per IP on disk and capped per day (IPQS_DAILY_CAP). Only if
  IPQS_API_KEY is set.

fraud_score is IPQS's 0-100, HIGHER = WORSE (75+ is their "suspicious" line).
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

IP_HEALTH_LOG = Path("logs/ip_health.jsonl")
_SCORE_CACHE = Path("logs/ip_scores.json")
_ABUSE_CACHE = Path("logs/abuse_scores.json")
_ABUSE_DAILY_CAP = int(os.environ.get("ABUSEIPDB_DAILY_CAP", 900))
_INFO_URL = "http://ip-api.com/json/?fields=query,isp,as,city,regionName,countryCode,hosting,proxy,mobile"

# IPQS free tier is ~1000/month; cap calls per UTC day (~1000/30) so rotating
# proxies can't burn the month in a week. Jobs past the cap keep fraud_score=None
# (ip-api's hosting/proxy flags still apply) and the dashboard averages the scored sample.
_DAILY_CAP = int(os.environ.get("IPQS_DAILY_CAP", 33))


# ponytail: whole-file rewrite per new IP; fine for hundreds of IPs, move to sqlite past ~50k.
def _load_cache(path: Path = _SCORE_CACHE) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _cached(path: Path, ip: str, cap: int, max_age: float | None, fetch) -> int | None:
    """Score `ip` via fetch(ip), cached on disk at `path` with a per-day call
    cap. max_age (seconds) makes older cache entries count as misses -- the
    reputation services update with a lag, so a re-check before trusting an IP
    again shouldn't be served a weeks-old number. None = trust the cache forever."""
    cache = _load_cache(path)
    scores, calls = cache.setdefault("scores", {}), cache.setdefault("calls", {})
    ts = cache.setdefault("ts", {})
    if ip in scores and (max_age is None or time.time() - ts.get(ip, 0) <= max_age):
        return scores[ip]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if calls.get(today, 0) >= cap:
        return None
    try:
        score = fetch(ip)
    except Exception:
        logger.warning("IP score lookup failed for %s", ip, exc_info=True)
        return None
    cache["calls"] = {today: calls.get(today, 0) + 1}  # only today matters; drops old days
    if score is not None:
        scores[ip], ts[ip] = score, time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache))
    return score


def fraud_score(ip: str, max_age: float | None = None) -> int | None:
    """IPQS fraud score 0-100 (higher = worse). None if no key/cap hit/error."""
    key = os.environ.get("IPQS_API_KEY", "").strip()
    if not key:
        return None

    def fetch(ip):
        url = f"https://www.ipqualityscore.com/api/json/ip/{key}/{ip}?strictness=1&allow_public_access_points=true"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        return data["fraud_score"] if data.get("success") else None

    return _cached(_SCORE_CACHE, ip, _DAILY_CAP, max_age, fetch)


def abuse_score(ip: str, max_age: float | None = None) -> int | None:
    """AbuseIPDB confidence-of-abuse 0-100 (higher = worse), the second
    opinion next to IPQS. Free tier is ~1000 checks/day. None if no key."""
    key = os.environ.get("ABUSEIPDB_API_KEY", "").strip()
    if not key:
        return None

    def fetch(ip):
        req = urllib.request.Request(
            f"https://api.abuseipdb.com/api/v2/check?ipAddress={ip}&maxAgeInDays=90",
            headers={"Key": key, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())["data"]["abuseConfidenceScore"]

    return _cached(_ABUSE_CACHE, ip, _ABUSE_DAILY_CAP, max_age, fetch)


def lookup(local_proxy_port: int, provider: str, kind: str, timeout: float = 5.0) -> dict | None:
    """Exit-IP identity + fraud score for whatever sits behind the local forwarder."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{local_proxy_port}"})
    )
    try:
        with opener.open(_INFO_URL, timeout=timeout) as resp:
            d = json.loads(resp.read())
        return {
            "ip": d["query"], "isp": d.get("isp"), "asn": (d.get("as") or "").split(" ")[0] or None,
            "city": d.get("city"), "region": d.get("regionName"), "country": d.get("countryCode"),
            "hosting": bool(d.get("hosting")), "proxy_flag": bool(d.get("proxy")),
            "mobile": bool(d.get("mobile")),
            "fraud_score": fraud_score(d["query"]), "provider": provider, "kind": kind,
        }
    except Exception:
        logger.warning("ip_health lookup failed (%s)", provider, exc_info=True)
        return None


# Outcomes that mean the site or the proxy stopped us, as opposed to a dead
# posting or an eligibility miss -- those say nothing about the IP.
_BLOCK_CATEGORIES = {"captcha", "site_blocked", "proxy_dropped"}


def _blocked(status: str) -> bool:
    from applypilot.apply.failure_taxonomy import normalize_failure_reason

    if status == "captcha":
        return True
    return status.startswith("failed:") and normalize_failure_reason(status[7:]) in _BLOCK_CATEGORIES


def log_job(job: dict, worker_id: int, status: str, dry_run: bool, info: dict | None) -> None:
    """Append one run. info=None (direct / lookup failed) still logs, so
    block rate for un-attributable runs isn't silently dropped."""
    try:
        IP_HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(), "url": job.get("url"),
            "ats": job.get("ats") or job.get("site"), "worker_id": worker_id,
            "dry_run": dry_run, "status": status, "applied": status == "applied",
            "blocked": _blocked(status), **(info or {"provider": "direct", "kind": "direct"}),
        }
        with IP_HEALTH_LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        logger.debug("ip_health: failed to log", exc_info=True)
