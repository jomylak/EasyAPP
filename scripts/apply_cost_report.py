"""Read-only apply-cost report: is the fleet getting cheaper, and is it
actually paying what it should.

Run before and after a cost change (provider pinning, prompt trims, etc.) to
measure the effect, per the [[applypilot-known-quirks]] gap this closes: the
known_quirks/known_issues caches kept growing with nothing checking whether
turns-per-ATS actually went down afterward.

Prints two DB-backed sections always (turns/cost/success per ATS per week,
and actual-vs-predicted cost ratio per run). The Langfuse section (calls per
tool, repeat rate, top repeated (tool, args) pairs) only prints if
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are set -- it hits Langfuse's public
API directly rather than requiring the langfuse SDK as a new dependency.

Usage: python scripts/apply_cost_report.py [weeks_back]
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from applypilot.config import load_env, ensure_dirs

load_env()
ensure_dirs()

from applypilot.database import get_connection, init_db  # noqa: E402
from applypilot.costs import price_for, observed_costs  # noqa: E402

init_db()

WEEKS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round(pct * (len(s) - 1))))
    return s[idx]


def _week_start(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Section 1: per-ATS per-week turns/cost/success
# ---------------------------------------------------------------------------

def print_weekly_ats_report(conn) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=WEEKS_BACK)).isoformat()
    rows = conn.execute("""
        SELECT ats, apply_backend AS backend, apply_status AS status,
               apply_cost_usd AS cost, apply_llm_requests AS turns,
               applied_at, last_attempted_at
        FROM jobs
        WHERE apply_backend = 'goose' AND apply_status IS NOT NULL
              AND apply_status != 'in_progress'
              AND COALESCE(applied_at, last_attempted_at) >= ?
    """, (cutoff,)).fetchall()

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        ts = r["applied_at"] or r["last_attempted_at"]
        if not ts:
            continue
        groups[(r["ats"] or "unknown", _week_start(ts))].append(r)

    print(f"\n=== Per ATS per week (last {WEEKS_BACK} weeks, goose only) ===")
    print(f"{'ATS':<20} {'week':<11} {'runs':>5} {'turns_p50':>10} {'turns_p90':>10}"
          f" {'cost_p50':>9} {'cost_p90':>9} {'success%':>9}")
    for (ats, week), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        turns = [r["turns"] for r in rs if r["turns"] is not None]
        costs_ = [r["cost"] for r in rs if r["cost"] is not None]
        n_applied = sum(1 for r in rs if r["status"] == "applied")
        print(f"{ats:<20} {week:<11} {len(rs):>5} "
              f"{median(turns) if turns else 0:>10.0f} {_percentile(turns, 0.9) or 0:>10.0f} "
              f"${median(costs_) if costs_ else 0:>7.3f} ${_percentile(costs_, 0.9) or 0:>7.3f} "
              f"{100 * n_applied / len(rs):>8.0f}%")


# ---------------------------------------------------------------------------
# Section 2: actual-vs-predicted cost ratio (catches provider drift)
# ---------------------------------------------------------------------------

def print_predicted_ratio_report(conn) -> None:
    observed = observed_costs(conn)
    rows = conn.execute("""
        SELECT title, ats, apply_backend AS backend, apply_cost_usd AS cost
        FROM jobs
        WHERE apply_backend IS NOT NULL AND apply_cost_usd IS NOT NULL
    """).fetchall()

    ratios = []
    for r in rows:
        predicted, n, _basis = price_for(r["backend"], r["ats"], observed)
        if predicted:
            ratios.append((r["cost"] / predicted, r["title"], r["ats"], r["cost"], predicted))

    print(f"\n=== Actual/predicted cost ratio (n={len(ratios)}) ===")
    if not ratios:
        print("no runs with recorded cost")
        return
    vals = [x[0] for x in ratios]
    print(f"median ratio: {median(vals):.2f}x   p90: {_percentile(vals, 0.9):.2f}x")
    outliers = sorted((x for x in ratios if x[0] >= 1.3), key=lambda x: -x[0])[:10]
    if outliers:
        print("Runs at >= 1.3x predicted (provider drift candidates):")
        for ratio, title, ats, cost, predicted in outliers:
            print(f"  {ratio:>5.2f}x  ${cost:.3f} vs ${predicted:.3f} predicted"
                  f"  [{ats}] {title[:50]}")
    else:
        print("no runs at or above 1.3x predicted")


# ---------------------------------------------------------------------------
# Section 3: Langfuse tool-call stats (optional -- needs API keys)
# ---------------------------------------------------------------------------

def _langfuse_get(path: str, params: dict) -> dict:
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    public = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret = os.environ["LANGFUSE_SECRET_KEY"]
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    # urlencode is load-bearing, not cosmetic: an unescaped "+00:00" UTC
    # offset in fromStartTime gets decoded server-side as a literal space,
    # which Langfuse's strict ISO-datetime regex then rejects with a 400.
    qs = urllib.parse.urlencode(params)
    url = f"{host}{path}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())




def _change_windows(since: str) -> list[tuple[str, str]]:
    """(iso_ts, label) per deploy that changed the agent's prompt or tools.

    Written by scripts/deploy_to_vm.sh, so before/after comparison needs no
    separate experiment ledger -- deploying is the experiment.
    """
    log = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "agent_deploys.tsv")
    wins = [(since, "(window start)")]
    if os.path.exists(log):
        with open(log) as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[0] > since:
                    # 4th column (the deploy note) says what changed; older
                    # lines only have the commit subject.
                    wins.append((parts[0], f"{parts[1]} {parts[3] if len(parts) > 3 else parts[2]}"))
    return sorted(wins)


def _err_class(obs: dict) -> str | None:
    out = obs.get("output")
    if not isinstance(out, dict) or out.get("status") != "error":
        return None
    blob = json.dumps(out, default=str)
    for needle, label in (
        ("ECONNREFUSED", "browser_dead"),
        ("not found in the current page snapshot", "stale_ref"),
        ("strict mode violation", "ambiguous_selector"),
        ("imeout", "timeout"),
        ("not found", "not_found"),
    ):
        if needle in blob:
            return label
    return "other"


def _fetch_tool_calls(since: str) -> list[dict]:
    """Goose emits one `dispatch_tool_call` span per tool call; the tool's own
    name lives in metadata, not in the span name."""
    calls, page = [], 1
    while True:
        data = _langfuse_get("/api/public/observations", {
            "name": "dispatch_tool_call", "fromStartTime": since,
            "page": page, "limit": 100,
        })
        items = data.get("data", [])
        calls.extend(items)
        meta = data.get("meta", {})
        if not items or page >= meta.get("totalPages", page):
            return calls
        page += 1


def _summarize(calls: list[dict], label: str) -> None:
    if not calls:
        print(f"  {label}: no calls")
        return
    # A run whose Chrome never came up burns ~6 turns failing to connect; those
    # are an infra fault, and averaging them in hides every prompt-level signal.
    dead_runs = {c["traceId"] for c in calls if _err_class(c) == "browser_dead"}
    live = [c for c in calls if c["traceId"] not in dead_runs]
    runs = {c["traceId"] for c in live}
    errs = [e for e in (_err_class(c) for c in live) if e]
    per_tool: Counter = Counter(
        (c.get("metadata") or {}).get("gen_ai.tool.name", "unknown") for c in live
    )
    tool_errs: Counter = Counter(
        (c.get("metadata") or {}).get("gen_ai.tool.name", "unknown")
        for c in live if _err_class(c)
    )
    n_runs = max(len(runs), 1)
    print(f"  {label}")
    print(f"    runs {len(runs)}  calls/run {len(live) / n_runs:.1f}  "
          f"err {100 * len(errs) / max(len(live), 1):.1f}%  "
          f"dead-browser runs {len(dead_runs)}")
    if errs:
        print("    errors: " + "  ".join(
            f"{k}={v}" for k, v in Counter(errs).most_common()))
    for tool, n in per_tool.most_common(10):
        e = tool_errs[tool]
        print(f"      {n / n_runs:>6.1f}/run  {tool:<38} {n:>5} calls"
              + (f"  {100 * e / n:.0f}% err" if e else ""))


def print_openrouter_coverage() -> None:
    """What OpenRouter actually billed vs what we attributed to a job.

    The per-run number is trustworthy -- goose's `cost_usd` reproduces
    OpenRouter's billed total to the cent (verified 2026-09-17 against
    /api/v1/generation for 151 generation ids). What is NOT trustworthy is
    coverage: a run killed before goose emits its `complete` line, and every
    goose_quicktest.sh run, is billed by OpenRouter and recorded nowhere.
    This is the only number that catches that.
    """
    print("\n=== OpenRouter billed vs attributed (lifetime) ===")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("skipped: OPENROUTER_API_KEY not set")
        return
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            billed = json.loads(resp.read())["data"]["total_usage"]
    except Exception as exc:
        print(f"skipped: {exc}")
        return

    conn = get_connection()
    attributed = conn.execute(
        "SELECT COALESCE(SUM(apply_cost_usd), 0) FROM jobs WHERE apply_backend = 'goose'"
    ).fetchone()[0]
    print(f"OpenRouter billed:  ${billed:.2f}")
    print(f"attributed to jobs: ${attributed:.2f}  ({100 * attributed / billed:.0f}% coverage)"
          if billed else "no OpenRouter spend yet")
    if billed:
        print(f"unattributed:       ${billed - attributed:.2f}"
              "  (killed runs + quicktests; the claude backend bills Anthropic, not here)")


def print_langfuse_report() -> None:
    print("\n=== Langfuse tool-call stats ===")
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        print("skipped: LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY not set"
              " (they live in ~/.applypilot/.env on the VM)")
        return

    since = (datetime.now(timezone.utc) - timedelta(weeks=WEEKS_BACK)).isoformat()
    calls = _fetch_tool_calls(since)
    _summarize(calls, f"all runs since {since[:10]}")

    print("\n--- split by prompt/tool deploy (logs/agent_deploys.tsv) ---")
    windows = _change_windows(since)
    for i, (ts, label) in enumerate(windows):
        end = windows[i + 1][0] if i + 1 < len(windows) else "9999"
        bucket = [c for c in calls if ts <= c["startTime"] < end]
        _summarize(bucket, f"[{ts[:16]}] {label}")


if __name__ == "__main__":
    conn = get_connection()
    print_weekly_ats_report(conn)
    print_predicted_ratio_report(conn)
    print_openrouter_coverage()
    print_langfuse_report()
