import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { AtsStat } from "@/lib/types"

/**
 * Success rate and cost per ATS/backend. A table with an inline bar per row,
 * not a row of small donuts -- comparing several proportions by angle across
 * separate charts is exactly the "pie chart forest" anti-pattern; a sortable
 * bar-per-row table is the legible version of the same comparison.
 */
export function AtsBreakdown() {
  const [rows, setRows] = useState<AtsStat[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .atsStats()
      .then((res) => setRows(res.rows))
      .catch((e) => setError(String(e)))
  }, [])

  return (
    <section className="day-panel dash-area-ats">
      <div className="dayhead">
        <span className="dayname">Success rate by ATS</span>
        <span className="daycount">{rows ? `${rows.length} combinations` : "loading…"}</span>
      </div>
      {error && <div style={{ padding: 12, color: "var(--a-bad)" }}>Failed to load: {error}</div>}
      {rows && rows.length === 0 && (
        <div style={{ padding: 12, color: "var(--a-text-3)" }}>No completed apply runs yet.</div>
      )}
      {rows && rows.length > 0 && (
        <div className="dash-scroll" style={{ padding: "4px 16px 14px" }}>
          {rows.map((r) => (
            <div key={`${r.ats}-${r.backend}`} className="ats-row">
              <span className="ats-name" title={`${r.ats} · ${r.backend}`}>
                {r.ats} <span className="ats-backend">· {r.backend}</span>
              </span>
              <div className="ats-bar-track">
                <div
                  className="ats-bar-fill"
                  style={{ width: `${Math.round(r.success_rate * 100)}%` }}
                />
              </div>
              <span className="ats-pct">{Math.round(r.success_rate * 100)}%</span>
              <span className="ats-n">{r.n_applied}/{r.n_runs}</span>
              <span className="ats-cost">
                {r.median_cost_usd === null ? "—" : `$${r.median_cost_usd.toFixed(2)} med`}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
