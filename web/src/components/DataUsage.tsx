import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { DataStats } from "@/lib/types"

/**
 * Proxy bandwidth per apply run: headline numbers plus a histogram of MB/job
 * (the "bell curve"), with p50/p90 marked so an outlier tail is obvious.
 */
export function DataUsage() {
  const [d, setD] = useState<DataStats | null>(null)

  useEffect(() => {
    api.dataStats().then(setD).catch(() => setD(null))
  }, [])

  if (!d || d.jobs === 0) {
    return <div className="data-usage tile-sub">No per-job data usage logged yet.</div>
  }

  const hist = d.histogram
  const maxN = Math.max(...hist.map((b) => b.n), 1)
  const hi = hist[hist.length - 1].hi
  const pos = (mb: number) => `${Math.min(100, (mb / hi) * 100)}%`

  return (
    <div className="data-usage">
      <div className="data-usage-title">Data usage <span>per apply run, via proxy</span></div>
      <div className="data-usage-stats">
        <Stat label="Total" value={`${d.total_gb.toFixed(2)} GB`} />
        <Stat label="Avg / job" value={`${d.avg_mb.toFixed(1)} MB`} />
        <Stat label="p50" value={`${d.p50_mb.toFixed(1)} MB`} />
        <Stat label="p90" value={`${d.p90_mb.toFixed(1)} MB`} warn={d.p90_mb > 3 * d.p50_mb} />
        <Stat label="Max" value={`${d.max_mb.toFixed(1)} MB`} />
      </div>
      <div className="hist">
        <div className="hist-bars">
          {hist.map((b, i) => (
            <div
              key={i}
              className="hist-bar"
              style={{ height: b.n ? `${Math.max(6, (b.n / maxN) * 100)}%` : 0 }}
              title={`${b.lo.toFixed(1)}–${b.hi.toFixed(1)} MB: ${b.n} jobs`}
            />
          ))}
          <Marker left={pos(d.p50_mb)} label="p50" />
          <Marker left={pos(d.p90_mb)} label="p90" />
        </div>
        <div className="hist-axis">
          <span>0</span>
          <span>MB per job</span>
          <span>{hi.toFixed(0)}+</span>
        </div>
      </div>
    </div>
  )
}

function Marker({ left, label }: { left: string; label: string }) {
  return (
    <div className="hist-marker" style={{ left }}>
      <span>{label}</span>
    </div>
  )
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <div className="data-label">{label}</div>
      <div className="data-value" style={warn ? { color: "var(--a-warn)" } : undefined}>{value}</div>
    </div>
  )
}
