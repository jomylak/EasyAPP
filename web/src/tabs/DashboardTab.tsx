import { useEffect, useState } from "react"
import { ApplicationsTable } from "@/components/ApplicationsTable"
import { AtsBreakdown } from "@/components/AtsBreakdown"
import { Donut } from "@/components/Donut"
import { api } from "@/lib/api"
import type { Stats } from "@/lib/types"
import { useRunStream } from "@/lib/useRunStream"

const STATUS_LABEL: Record<string, string> = {
  starting: "Starting",
  applying: "Applying",
  applied: "Applied",
  failed: "Failed",
  expired: "Expired",
  captcha: "Captcha",
  idle: "Idle",
  done: "Done",
}

function elapsed(startTime: number): string {
  if (!startTime) return "—"
  const s = Math.max(0, Math.round(Date.now() / 1000 - startTime))
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`
}

export function DashboardTab() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [statsError, setStatsError] = useState<string | null>(null)
  const { run, live } = useRunStream()
  const [stopping, setStopping] = useState(false)

  function refreshStats() {
    api
      .stats()
      .then((res) => {
        setStats(res.stats)
        setStatsError(null)
      })
      .catch((e) => setStatsError(String(e)))
  }

  useEffect(() => {
    refreshStats()
    const id = setInterval(refreshStats, 4000)
    return () => clearInterval(id)
  }, [])

  async function stopAll() {
    setStopping(true)
    try {
      await api.stopAll()
    } finally {
      setStopping(false)
      refreshStats()
    }
  }

  return (
    <div className="dash">
      {statsError && <div style={{ color: "var(--a-bad)" }}>Failed to load stats: {statsError}</div>}

      {stats && (
        <div className="tiles">
          <div className="tile">
            <div className="tile-label">Applied</div>
            <div className="tile-value good">{stats.applied}</div>
          </div>
          <div className="tile">
            <div className="tile-label">In flight</div>
            <div className="tile-value warn">{stats.in_progress}</div>
          </div>
          <div className="tile">
            <div className="tile-label">Failed</div>
            <div className="tile-value bad">{stats.failed}</div>
          </div>
          <div className="tile">
            <div className="tile-label">Queued</div>
            <div className="tile-value">{stats.queued}</div>
          </div>
          <div className="tile">
            <div className="tile-label">Spend</div>
            <div className="tile-value">${stats.spend.toFixed(2)}</div>
          </div>
          <div className="tile">
            <div className="tile-label">Scored</div>
            <div className="tile-value">{stats.scored}</div>
            <div className="tile-sub">{stats.pending_enrich} awaiting detail</div>
          </div>
        </div>
      )}

      {stats && (
        <div className="dash-grid">
          <section className="day-panel">
            <div className="dayhead">
              <span className="dayname">Outcomes</span>
            </div>
            <div className="donut-row">
              <Donut
                value={stats.applied + stats.failed > 0 ? stats.applied / (stats.applied + stats.failed) : 0}
                label="Success rate"
                sublabel={`${stats.applied} of ${stats.applied + stats.failed} attempts`}
                color="var(--a-good)"
              />
              <div>
                <div className="tile-label">Avg cost / attempt</div>
                <div className="tile-value" style={{ fontSize: 22, marginTop: 4 }}>
                  {/* priced_attempts, not applied + failed: retrying a failed
                      job flips it back to 'queued' (dropping it from that
                      count) without touching its already-spent cost, which
                      would otherwise make this number jump on a retry click
                      alone, before anything has actually run again. */}
                  {stats.priced_attempts > 0
                    ? `$${(stats.spend / stats.priced_attempts).toFixed(2)}`
                    : "—"}
                </div>
                <div className="tile-sub">${stats.spend.toFixed(2)} total spend</div>
              </div>
            </div>
          </section>
          <AtsBreakdown />
        </div>
      )}

      <div className="runpanel">
        <div className="runpanel-head">
          <span className={`live-dot${live ? " on" : ""}`} />
          <span style={{ fontFamily: "var(--mono)", fontSize: 12.5, fontWeight: 600 }}>
            {live ? `Run in progress · batch ${run?.batch ?? "—"}` : "No run in progress"}
          </span>
          {run?.dry_run && <span className="badge manual">dry run</span>}
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
            {live && (
              <span style={{ fontSize: 11.5, color: "var(--a-text-3)", fontFamily: "var(--mono)" }}>
                {run?.totals.applied ?? 0} applied · {run?.totals.failed ?? 0} failed · $
                {(run?.totals.cost ?? 0).toFixed(2)}
              </span>
            )}
            <button className="btn" disabled={!live || stopping} onClick={stopAll}>
              {stopping ? "Stopping…" : "Stop all"}
            </button>
          </div>
        </div>

        {!live && <div className="runpanel-empty">Launch a queued batch from the terminal to see it here live.</div>}

        {live &&
          run?.workers.map((w) => (
            <div className="worker-row" key={w.worker_id}>
              <span className="worker-id">#{w.worker_id}</span>
              <span className="worker-status">
                <span className={`badge ${w.status === "applied" ? "applied" : w.status === "failed" ? "failed" : "in_progress"}`}>
                  {STATUS_LABEL[w.status] ?? w.status}
                </span>
              </span>
              <span className="worker-job" title={`${w.company} — ${w.job_title}`}>
                {w.company ? (
                  <span className={w.company_tier === "tier1" ? "tier1-company" : w.company_tier ? "tier-adjacent" : undefined}>
                    {w.company}
                  </span>
                ) : (
                  "—"
                )}{" "}
                {w.job_title ? `— ${w.job_title}` : ""}
              </span>
              <span className="worker-last" title={w.last_action}>
                {w.last_action || "—"}
              </span>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--a-text-3)" }}>
                {elapsed(w.start_time)}
              </span>
            </div>
          ))}
      </div>

      <ApplicationsTable live={live} />
    </div>
  )
}
