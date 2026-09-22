"""Print the fingerprint-check history table + trend -- see
applypilot.apply.fingerprint_history for the shared implementation (also
used by the web dashboard's /api/fingerprint-history).

Usage:
    python scripts/fingerprint_history.py
"""
from applypilot.apply.fingerprint_history import REPORTS_DIR, load_rows, trend


def main() -> None:
    rows = load_rows()
    if not rows:
        print(f"No reports in {REPORTS_DIR} yet -- run fingerprint_check.py first.")
        return

    header = ("Time", "Label", "Route", "Headless%", "WebRTC leak", "WebGL renderer")
    widths = [max(len(str(r[i])) for r in ([header] + rows)) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*header))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*[str(x) for x in r]))

    print()
    by_route: dict[str, list[int]] = {}
    for r in rows:
        if r[3] is not None:
            by_route.setdefault(r[2], []).append(r[3])
    for route, vals in by_route.items():
        print(f"avg headless% ({route}, n={len(vals)}): {sum(vals) / len(vals):.1f}%")

    t = trend(rows)
    if t:
        arrow = {"rising": "▲", "falling": "▼", "flat": "→"}[t["direction"]]
        print(f"latest run: {t['latest']}% vs prior avg {t['prior_avg']}% "
              f"({arrow} {t['direction']}, {t['delta']:+.1f}pt)")


if __name__ == "__main__":
    main()
