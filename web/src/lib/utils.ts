import { type ClassValue, clsx } from "clsx"
import { useEffect, useState } from "react"

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs)
}

// ---------------------------------------------------------------------------
// Score color ramps, ported byte-for-byte (as an interpolation, not a lookup
// table) from the approved mock. Prestige runs warm, fit runs cool -- they
// answer different questions and are deliberately not the same color.
// ---------------------------------------------------------------------------

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "")
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ]
}

function mix(a: string, b: string, t: number): string {
  const [r1, g1, b1] = hexToRgb(a)
  const [r2, g2, b2] = hexToRgb(b)
  const r = Math.round(r1 + (r2 - r1) * t)
  const g = Math.round(g1 + (g2 - g1) * t)
  const bl = Math.round(b1 + (b2 - b1) * t)
  return `rgb(${r},${g},${bl})`
}

const PRE_LO = "#4a3a22"
const PRE_HI = "#e6b268"
const FIT_LO = "#31465c"
const FIT_HI = "#7fbce8"

const clamp01 = (t: number) => Math.max(0, Math.min(1, t))

export function preColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "var(--a-text-3)"
  return mix(PRE_LO, PRE_HI, clamp01((v - 2) / 8))
}

export function fitColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "var(--a-text-3)"
  return mix(FIT_LO, FIT_HI, clamp01((v - 2) / 8))
}

// ---------------------------------------------------------------------------
// Description sanitising. Some rows store escaped markup rather than prose --
// Adobe's full_description begins `&lt;div&gt;&lt;p&gt;...`. Unescape entities
// then strip tags; never render this as HTML.
// ---------------------------------------------------------------------------

export function sanitizeDescription(raw: string | null | undefined): string {
  if (!raw) return ""
  // Unescape HTML entities via a detached template -- textContent decodes
  // entities but never executes anything, since the node is never attached
  // to the document and nothing is set via innerHTML.
  const unescape = (s: string): string => {
    const ta = document.createElement("textarea")
    ta.innerHTML = s
    return ta.value
  }

  let text = raw
  // Some rows are double-escaped (&amp;lt; -> &lt; -> <), so unescape twice.
  text = unescape(text)
  if (/&lt;|&gt;|&amp;/.test(text)) text = unescape(text)

  // Strip tags.
  text = text.replace(/<[^>]*>/g, " ")
  // Collapse whitespace left behind by stripped block tags.
  text = text.replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n\n").trim()
  return text
}

// ---------------------------------------------------------------------------
// Default page sizes by recency: 30 rows today, 20 for the last three days,
// 10 for older. `days` is the list from /api/days, newest first.
// ---------------------------------------------------------------------------

export function defaultPageSize(index: number): number {
  if (index === 0) return 30
  if (index <= 3) return 20
  return 10
}

export function formatDayLabel(day: string): string {
  // day comes back as an ISO date string (YYYY-MM-DD) from _DAY_EXPR.
  const d = new Date(`${day}T00:00:00`)
  if (Number.isNaN(d.getTime())) return day
  const weekday = d.toLocaleDateString(undefined, { weekday: "short" })
  const month = d.toLocaleDateString(undefined, { month: "short" })
  return `${weekday} · ${month} ${d.getDate()}`
}

// `posted` comes back from the API as an ISO timestamp (COALESCE(posted_date,
// discovered_at) -- see queries.py), so this always has a value to parse.
export function daysAgo(posted: string | null | undefined): number | null {
  if (!posted) return null
  const then = new Date(posted).getTime()
  if (Number.isNaN(then)) return null
  const diffMs = Date.now() - then
  return Math.max(0, Math.floor(diffMs / 86_400_000))
}

export function formatDaysAgo(posted: string | null | undefined): string {
  const d = daysAgo(posted)
  if (d === null) return "—"
  if (d === 0) return "today"
  if (d === 1) return "1d"
  return `${d}d`
}

export function formatPay(row: { pay_text?: string | null; salary?: number | null }): string {
  if (row.pay_text) return row.pay_text
  if (row.salary) return `$${Math.round(row.salary).toLocaleString()}`
  return "—"
}

export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(id)
  }, [value, delayMs])
  return debounced
}
