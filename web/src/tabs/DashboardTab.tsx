import { useEffect, useState } from "react"
import { ApplicationsTable } from "@/components/ApplicationsTable"
import { AtsBreakdown } from "@/components/AtsBreakdown"
import { CompanyLimits } from "@/components/CompanyLimits"
import { Donut } from "@/components/Donut"
import { FailureBreakdown } from "@/components/FailureBreakdown"
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
  // Batches someone queued (from Browse, or a Launch that queued fine but
  // then 409'd because a run was already going) that nobody has launched
  // yet -- the recovery path for that race, so the batch id in a toast
  // isn't the only way back to it.
  const [pending, setPending] = useState<{ batch: string; count: number; queued_at: string | null }[]>([])
  const [launchingBatch, setLaunchingBatch] = useState<string | null>(null)
  const [pendingError, setPendingError] = useState<string | null>(null)

  function refreshStats() {
    api
      .stats()
      .then((res) => {
        setStats(res.stats)
        setStatsError(null)
      })
      .catch((e) => setStatsError(String(e)))
  }

  function refreshPending() {
    api.pendingBatches().then((res) => setPending(res.batches)).catch(() => {})
  }

  useEffect(() => {
    refreshStats()
    refreshPending()
    const id = setInterval(() => {
      refreshStats()
      refreshPending()
    }, 4000)
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

  async function launchPending(batch: string) {
    setLaunchingBatch(batch)
    setPendingError(null)
    try {
      await api.launch(batch, { workers: 1 })
      refreshPending()
      refreshStats()
    } catch (e) {
      setPendingError(String(e))
    } finally {
      setLaunchingBatch(null)
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
            <div className="tile-label">Apply spend</div>
            <div className="tile-value">${stats.spend.toFixed(2)}</div>
          </div>
          <div className="tile">
            {/* Scoring cost never used to be tracked at all -- only apply
                (goose) spend was persisted, so switching the scoring model
                to something that runs against every job in the DB (not just
                the handful that go through apply) was invisible here. */}
            <div className="tile-label">Scoring spend</div>
            <div className="tile-value">${stats.scoring_spend.toFixed(2)}</div>
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
          <section className="day-panel dash-area-outcomes">
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
          <FailureBreakdown />
          <AtsBreakdown />
          <CompanyLimits />
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

        {!live && pending.length === 0 && (
          <div className="runpanel-empty">Launch a queued batch from the terminal to see it here live.</div>
        )}

        {!live &&
          pending.map((b) => (
            <div className="worker-row" key={b.batch}>
              <span className="worker-job" style={{ fontFamily: "var(--mono)" }}>
                {b.count} job(s) queued as <b>{b.batch}</b>
              </span>
              <button
                className="btn"
                disabled={launchingBatch === b.batch}
                onClick={() => launchPending(b.batch)}
              >
                {launchingBatch === b.batch ? "Launching…" : "Launch"}
              </button>
            </div>
          ))}

        {pendingError && (
          <div style={{ color: "var(--a-bad)", fontSize: 12, padding: "4px 0" }}>
            Failed to launch: {pendingError}
          </div>
        )}

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
