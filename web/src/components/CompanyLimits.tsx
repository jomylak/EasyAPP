import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { CompanyLimitStatus } from "@/lib/types"

/**
 * Per-company application cap: how many of a period's allowance are already
 * spent. Only companies with at least one applied row show up -- an unused
 * cap isn't worth a row. Sorted by fewest remaining first, same as
 * company_limits.all_statuses(), so the ones closest to blocking a future
 * apply are the ones seen first.
 */
export function CompanyLimits() {
  const [rows, setRows] = useState<CompanyLimitStatus[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .companyLimits()
      .then((res) => setRows(res.rows))
      .catch((e) => setError(String(e)))
  }, [])

  return (
    <section className="day-panel">
      <div className="dayhead">
        <span className="dayname">Company application caps</span>
        <span className="daycount">{rows ? `${rows.length} companies` : "loading…"}</span>
      </div>
      {error && <div style={{ padding: 12, color: "var(--a-bad)" }}>Failed to load: {error}</div>}
      {rows && rows.length === 0 && (
        <div style={{ padding: 12, color: "var(--a-text-3)" }}>No applications sent yet.</div>
      )}
      {rows && rows.length > 0 && (
        <div style={{ padding: "4px 16px 14px" }}>
          {rows.map((r) => (
            <div key={r.company} className="cl-row">
              <span className="cl-name" title={r.company ?? undefined}>
                {r.company}
              </span>
              <div className="cl-bar-track">
                <div
                  className={`cl-bar-fill${r.at_cap ? " at-cap" : r.remaining <= 1 ? " near-cap" : ""}`}
                  style={{ width: `${Math.min(100, Math.round((r.applied / r.limit) * 100))}%` }}
                />
              </div>
              <span className={`cl-count${r.at_cap ? " at-cap" : ""}`}>
                {r.applied}/{r.limit}
              </span>
              <span className="cl-period">{r.period === "total" ? "lifetime" : `per ${r.period}`}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
