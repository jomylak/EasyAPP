import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { IpStats, ProxySwaps } from "@/lib/types"

type GroupKey = keyof IpStats["groups"]
const GROUPS: { key: GroupKey; label: string }[] = [
  { key: "provider", label: "Provider" },
  { key: "isp", label: "ISP" },
  { key: "country", label: "Country" },
  { key: "city", label: "City" },
]

/** Webshare IP swaps: monthly quota left and the most recent replacements. */
function Swaps({ s }: { s: ProxySwaps }) {
  return (
    <div className="tile-sub">
      Proxy swaps: <b>{s.remaining}</b>/{s.quota} left this month
      {s.swaps.slice(-3).reverse().map((w) => (
        <div key={w.ts}>
          {w.old_ip} ({w.old_score ?? "?"}) → {w.new_ip ?? "?"} ({w.new_score ?? "?"})
        </div>
      ))}
    </div>
  )
}

const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`)

/**
 * Exit-IP health: fraud-score distribution (IPQualityScore, 0-100, higher is
 * worse) with everything at or past the cutoff drawn red, plus block/success
 * rate sliced by provider/ISP/country/city. The block rate is the real
 * signal; the fraud score is a proxy that's available before a job runs.
 */
export function IpHealth() {
  const [d, setD] = useState<IpStats | null>(null)
  const [group, setGroup] = useState<GroupKey>("provider")

  useEffect(() => {
    api.ipStats().then(setD).catch(() => setD(null))
  }, [])

  if (!d || d.jobs === 0) {
    return (
      <div className="data-usage">
        <div className="tile-sub">No per-job IP data logged yet.</div>
        {d?.swaps && <Swaps s={d.swaps} />}
      </div>
    )
  }

  const maxN = Math.max(...d.histogram, 1)
  const cutoffBin = Math.floor(d.cutoff / 10)
  const rows = d.groups[group]

  return (
    <div className="data-usage">
      <div className="data-usage-title">IP health <span>exit-IP fraud score, lower is better</span></div>
      <div className="data-usage-stats">
        <Stat
          label="Avg score"
          value={d.avg_score === null ? "—" : d.avg_score.toFixed(0)}
          bad={d.avg_score !== null && d.avg_score >= d.cutoff}
        />
        <Stat label="IPs" value={String(d.ips)} />
        <Stat label={`≥${d.cutoff}`} value={pct(d.pct_over_cutoff)} bad={(d.pct_over_cutoff ?? 0) > 0.1} />
        <Stat label="Block rate" value={pct(d.block_rate)} bad={(d.block_rate ?? 0) > 0.15} />
      </div>
      {d.scored === 0 ? (
        <div className="tile-sub">No fraud scores yet -- set IPQS_API_KEY on the VM.</div>
      ) : (
        <div className="hist">
          <div className="hist-bars">
            {d.histogram.map((n, i) => (
              <div
                key={i}
                className={`hist-bar${i >= cutoffBin ? " bad" : ""}`}
                style={{ height: n ? `${Math.max(6, (n / maxN) * 100)}%` : 0 }}
                title={`score ${i * 10}–${i * 10 + 9}: ${n} jobs`}
              />
            ))}
            <div className="hist-marker" style={{ left: `${d.cutoff}%` }}>
              <span>cutoff</span>
            </div>
          </div>
          <div className="hist-axis">
            <span>0</span>
            <span>fraud score</span>
            <span>100</span>
          </div>
        </div>
      )}
      {d.swaps && <Swaps s={d.swaps} />}
      <div className="ip-tabs">
        {GROUPS.map((g) => (
          <button key={g.key} className={`ip-tab${group === g.key ? " on" : ""}`} onClick={() => setGroup(g.key)}>
            {g.label}
          </button>
        ))}
      </div>
      <div className="ip-row ip-head">
        <span>{GROUPS.find((g) => g.key === group)?.label}</span>
        <span>jobs</span>
        <span>ok</span>
        <span>blocked</span>
        <span>score</span>
      </div>
      {rows.map((r) => (
        <div key={r.name} className="ip-row">
          <span className="ip-name" title={r.name}>{r.name}</span>
          <span>{r.n}</span>
          <span>{pct(r.success_rate)}</span>
          <span className={r.block_rate > 0.15 ? "ip-bad" : undefined}>{pct(r.block_rate)}</span>
          <span className={r.avg_score !== null && r.avg_score >= d.cutoff ? "ip-bad" : undefined}>
            {r.avg_score === null ? "—" : r.avg_score.toFixed(0)}
          </span>
        </div>
      ))}
    </div>
  )
}

function Stat({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div>
      <div className="data-label">{label}</div>
      <div className="data-value" style={bad ? { color: "var(--a-bad)" } : undefined}>{value}</div>
    </div>
  )
}
