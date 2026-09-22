import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { FingerprintTrend } from "@/lib/types"

/**
 * Latest CreepJS "like headless" % + trend vs. every prior run -- an
 * environment health check (Chrome build, stealth extension, proxy
 * reputation), not per-job telemetry. See scripts/fingerprint_check.py and
 * applypilot.apply.fingerprint_history for why this only moves on a deploy
 * or the daily applypilot-fingerprint.timer, not on every apply.
 */
export function FingerprintTile() {
  const [trend, setTrend] = useState<FingerprintTrend | null | undefined>(undefined)

  useEffect(() => {
    api
      .fingerprintHistory()
      .then((res) => setTrend(res.trend))
      .catch(() => setTrend(null))
  }, [])

  if (trend === undefined) return null
  if (trend === null) {
    return (
      <div className="tile">
        <div className="tile-label">Bot-likeness</div>
        <div className="tile-value">—</div>
        <div className="tile-sub">not enough fingerprint_check.py runs yet</div>
      </div>
    )
  }

  const arrow = { rising: "▲", falling: "▼", flat: "→" }[trend.direction]
  const cls = trend.direction === "rising" ? "bad" : trend.direction === "falling" ? "good" : ""

  return (
    <div className="tile" title="CreepJS 'like headless' %, lower is better -- see fingerprint_check.py">
      <div className="tile-label">Bot-likeness</div>
      <div className={`tile-value ${cls}`}>{trend.latest}%</div>
      <div className="tile-sub">
        {arrow} vs {trend.prior_avg}% avg (n={trend.n})
      </div>
    </div>
  )
}
