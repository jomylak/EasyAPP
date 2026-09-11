import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { FailureReasonStat } from "@/lib/types"

/**
 * Count of failed apply attempts per canonical failure category -- a
 * bar-per-row table, same shape as AtsBreakdown, rather than a donut/pie:
 * this is N categories compared by magnitude, not one proportion against its
 * complement, and stacking several wedges by angle is the exact "pie chart
 * forest" anti-pattern Donut's own docstring calls out. Categories come
 * pre-normalized from the backend (apply.failure_taxonomy) so near-duplicate
 * raw reason strings (an Indeed-specific block message vs. a generic one)
 * already collapse into one bar before this ever renders.
 */
export function FailureBreakdown() {
  const [rows, setRows] = useState<FailureReasonStat[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .failureReasons()
      .then((res) => setRows(res.rows))
      .catch((e) => setError(String(e)))
  }, [])

  const max = rows && rows.length > 0 ? Math.max(...rows.map((r) => r.n)) : 0

  return (
    <section className="day-panel dash-area-failures">
      <div className="dayhead">
        <span className="dayname">Failure reasons</span>
        <span className="daycount">{rows ? `${rows.length} categories` : "loading…"}</span>
      </div>
      {error && <div style={{ padding: 12, color: "var(--a-bad)" }}>Failed to load: {error}</div>}
      {rows && rows.length === 0 && (
        <div style={{ padding: 12, color: "var(--a-text-3)" }}>No failed apply attempts yet.</div>
      )}
      {rows && rows.length > 0 && (
        <div className="dash-scroll" style={{ padding: "4px 16px 14px" }}>
          {rows.map((r) => (
            <div key={r.category} className="fr-row">
              <span className="fr-name" title={r.label}>
                {r.label}
              </span>
              <div className="fr-bar-track">
                <div className="fr-bar-fill" style={{ width: `${max ? Math.round((r.n / max) * 100) : 0}%` }} />
              </div>
              <span className="fr-n">{r.n}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
