"""What a batch of applications is about to cost.

Nothing in the pipeline predicted cost before this: the backends report what a
run actually spent and `mark_result` records it, but there was no way to ask
"what will these twenty jobs cost me" before committing to them. The web UI
needs exactly that, because a person ticking checkboxes is spending money and
should be told how much before they press Launch.

The estimate is measured, not modelled. It reads the runs that already
happened and takes their median, narrowing to the same ATS where there is
enough history to justify it. Where there is no history it falls back to the
seeded defaults in settings.json, and it always reports how many samples it
had, because an estimate drawn from three runs and one drawn from three
hundred are not the same claim and the interface must not present them alike.
"""

import logging
import sqlite3
from statistics import median

from applypilot import config
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

# Below this many observations for an (backend, ats) pair, the pair's own
# median is too noisy to prefer over the backend-wide one. Three is not a
# statistically meaningful threshold; it is the point at which a single
# unusual run stops dominating the answer.
_MIN_ATS_SAMPLES = 3

# Spread applied to the expected value when reporting a range. Real per-run
# costs vary by more than this, but a wider band would be uninformative -- the
# honest signal about uncertainty is n_samples, not the width of the bar.
_SPREAD = 0.4


def _fallback_price(backend: str) -> float:
    defaults = config.load_settings().get("cost_defaults", {})
    if backend in defaults:
        return float(defaults[backend])
    # An unrecognised backend is worth flagging rather than silently pricing
    # at zero, which would render as a free batch.
    logger.warning("No cost default for backend %r; using the highest known.", backend)
    return float(max(defaults.values(), default=1.0))


def observed_costs(conn: sqlite3.Connection | None = None) -> dict:
    """Per-backend and per-(backend, ats) medians from completed runs.

    Only rows that actually recorded a cost count. A run that failed still
    cost money and is deliberately included -- excluding failures would price
    a batch as if every application succeeded, which is the optimistic
    direction and the wrong one to be wrong in.
    """
    conn = conn or get_connection()
    rows = conn.execute("""
        SELECT apply_backend AS backend, ats, apply_cost_usd AS cost
        FROM jobs
        WHERE apply_cost_usd IS NOT NULL AND apply_backend IS NOT NULL
    """).fetchall()

    by_backend: dict[str, list[float]] = {}
    by_pair: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        by_backend.setdefault(r["backend"], []).append(r["cost"])
        if r["ats"]:
            by_pair.setdefault((r["backend"], r["ats"]), []).append(r["cost"])

    return {
        "by_backend": {k: (median(v), len(v)) for k, v in by_backend.items()},
        "by_pair": {k: (median(v), len(v)) for k, v in by_pair.items()},
    }


def price_for(backend: str, ats: str | None, observed: dict) -> tuple[float, int, str]:
    """Best available per-application price, with its provenance.

    Returns (price, n_samples, basis) where basis names where the number came
    from, so the caller can show it rather than presenting every estimate with
    the same confidence.
    """
    pair = observed["by_pair"].get((backend, ats or ""))
    if pair and pair[1] >= _MIN_ATS_SAMPLES:
        return pair[0], pair[1], f"{backend} on {ats}"

    backend_wide = observed["by_backend"].get(backend)
    if backend_wide:
        return backend_wide[0], backend_wide[1], f"{backend}, all sites"

    return _fallback_price(backend), 0, "configured default"


def ats_stats(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Per-(ATS, backend) run statistics, for cost/duration reporting.

    This is the raw material `estimate_batch` already summarizes into a
    single price -- broken out here so it can be inspected directly (`applypilot
    ats-stats`) instead of only ever being consumed as a blended number. Every
    completed run counts, success or failure, because a failed run still spent
    tokens and wall-clock time and hiding that would understate real cost.
    """
    conn = conn or get_connection()
    rows = conn.execute("""
        SELECT ats, apply_backend AS backend, apply_status AS status,
               apply_cost_usd AS cost, apply_duration_ms AS duration_ms,
               apply_llm_requests AS llm_requests,
               apply_input_tokens AS input_tokens,
               apply_output_tokens AS output_tokens,
               apply_cache_read_tokens AS cache_read_tokens
        FROM jobs
        WHERE apply_backend IS NOT NULL AND apply_status IS NOT NULL
              AND apply_status != 'in_progress'
    """).fetchall()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r["ats"] or "unknown", r["backend"]), []).append(r)

    def _median_of(key: str, rs: list[sqlite3.Row]) -> float | None:
        vals = [r[key] for r in rs if r[key] is not None]
        return median(vals) if vals else None

    out = []
    for (ats, backend), rs in groups.items():
        n = len(rs)
        n_applied = sum(1 for r in rs if r["status"] == "applied")
        costs_ = [r["cost"] for r in rs if r["cost"] is not None]
        out.append({
            "ats": ats,
            "backend": backend,
            "n_runs": n,
            "n_applied": n_applied,
            "success_rate": round(n_applied / n, 3) if n else 0.0,
            "median_cost_usd": _median_of("cost", rs),
            "total_cost_usd": round(sum(costs_), 4) if costs_ else 0.0,
            "median_duration_s": (
                round(_median_of("duration_ms", rs) / 1000, 1)
                if _median_of("duration_ms", rs) is not None else None
            ),
            "median_llm_requests": _median_of("llm_requests", rs),
            "median_input_tokens": _median_of("input_tokens", rs),
            "median_output_tokens": _median_of("output_tokens", rs),
            "median_cache_read_tokens": _median_of("cache_read_tokens", rs),
        })

    out.sort(key=lambda r: (-r["n_runs"], r["ats"], r["backend"]))
    return out


def estimate_batch(urls: list[str], backend: str,
                   conn: sqlite3.Connection | None = None) -> dict:
    """Estimate what applying to these jobs will cost.

    Args:
        urls: The jobs the user selected.
        backend: Which apply backend will run them.
        conn: Database connection. Uses get_connection() if None.

    Returns:
        dict with `expected`, `low`, `high` (USD), `n_jobs`, `n_samples`
        (observations behind the estimate -- 0 means it is a configured guess)
        and `basis` (a short human-readable provenance string).
    """
    if not urls:
        return {"expected": 0.0, "low": 0.0, "high": 0.0,
                "n_jobs": 0, "n_samples": 0, "basis": "nothing selected"}

    conn = conn or get_connection()
    observed = observed_costs(conn)

    placeholders = ",".join("?" * len(urls))
    rows = conn.execute(
        f"SELECT url, ats FROM jobs WHERE url IN ({placeholders})", urls
    ).fetchall()
    # A URL with no row still costs something if it somehow reaches the
    # launcher, so price it at the backend-wide rate rather than dropping it.
    ats_by_url = {r["url"]: r["ats"] for r in rows}

    total = 0.0
    samples: list[int] = []
    bases: set[str] = set()
    for url in urls:
        price, n, basis = price_for(backend, ats_by_url.get(url), observed)
        total += price
        samples.append(n)
        bases.add(basis)

    # The weakest evidence behind any single job is the honest headline: a
    # batch is only as well-estimated as its least-known member.
    n_samples = min(samples) if samples else 0
    basis = bases.pop() if len(bases) == 1 else f"mixed ({len(bases)} sources)"

    return {
        "expected": round(total, 2),
        "low": round(total * (1 - _SPREAD), 2),
        "high": round(total * (1 + _SPREAD), 2),
        "n_jobs": len(urls),
        "n_samples": n_samples,
        "basis": basis,
    }
