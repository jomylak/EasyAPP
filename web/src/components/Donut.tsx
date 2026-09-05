interface Props {
  value: number // 0..1
  label: string
  sublabel?: string
  color?: string
  size?: number
}

/**
 * A single-value proportion ring -- the one legitimate use of a donut shape:
 * one series against its own complement, not several categories compared by
 * angle (that's the "pie chart forest" anti-pattern; the per-ATS breakdown
 * below uses a sortable table with inline bars instead, for exactly that
 * reason).
 */
export function Donut({ value, label, sublabel, color = "var(--a-good)", size = 96 }: Props) {
  const stroke = 10
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const pct = Math.max(0, Math.min(1, value))
  const dash = `${c * pct} ${c * (1 - pct)}`

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ transform: "rotate(-90deg)" }}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--a-line)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeDasharray={dash}
          strokeLinecap="round"
        />
      </svg>
      <div>
        <div style={{ fontFamily: "var(--mono)", fontSize: 22, fontWeight: 600, color: "var(--a-text)" }}>
          {Math.round(pct * 100)}%
        </div>
        <div style={{ fontSize: 12, color: "var(--a-text-2)" }}>{label}</div>
        {sublabel && <div style={{ fontSize: 11, color: "var(--a-text-3)", marginTop: 2 }}>{sublabel}</div>}
      </div>
    </div>
  )
}
