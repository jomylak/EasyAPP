"""Read back scripts/fingerprint_check.py's saved reports and trend the one
number worth trending (CreepJS's "like headless" %) over time.

Shared by scripts/fingerprint_history.py (CLI table) and the web dashboard's
/api/fingerprint-history endpoint -- one implementation, two callers.
"""
import json
from datetime import datetime

from applypilot import config

REPORTS_DIR = config.APP_DIR / "fingerprint_reports"


def load_rows() -> list[tuple]:
    """Every saved report as (time, label, route, headless%, webrtc leak,
    webgl renderer), oldest first.
    """
    reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime) \
        if REPORTS_DIR.exists() else []
    rows = []
    for path in reports:
        d = json.loads(path.read_text())
        creepjs = d.get("creepjs") or {}
        webgl = d.get("webgl") or {}
        rows.append((
            datetime.fromtimestamp(d["timestamp"]).strftime("%Y-%m-%d %H:%M"),
            d.get("label", "?"),
            "proxy" if d.get("via_proxy") else "direct",
            creepjs.get("pct_like_headless"),
            creepjs.get("webrtc_leaked_local_ip") or "none",
            webgl.get("renderer") or "none",
        ))
    return rows


def trend(rows: list[tuple]) -> dict | None:
    """Latest headless% vs. the average of every prior run, or None if
    there isn't enough history yet to compare against.
    """
    scored = [r for r in rows if r[3] is not None]
    if len(scored) < 2:
        return None
    latest = scored[-1][3]
    prior = [r[3] for r in scored[:-1]]
    prior_avg = sum(prior) / len(prior)
    delta = latest - prior_avg
    direction = "rising" if delta > 2 else "falling" if delta < -2 else "flat"
    return {"latest": latest, "prior_avg": round(prior_avg, 1), "delta": round(delta, 1),
            "direction": direction, "n": len(scored)}
