import type { Facets, GlobalFilters } from "@/lib/types"

interface Props {
  facets: Facets | null
  filters: GlobalFilters
  onChange: (next: GlobalFilters) => void
}

/**
 * The "what am I willing to look at at all" layer -- narrows every day table
 * underneath it. Scoped filters (prestige/fit/desirability/pay/search) live
 * per-day in DayTable; this bar only carries the ones that make sense to set
 * once for the whole browse session.
 */
export function GlobalFilterBar({ facets, filters, onChange }: Props) {
  function set<K extends keyof GlobalFilters>(key: K, value: GlobalFilters[K]) {
    onChange({ ...filters, [key]: value })
  }

  return (
    <div className="gfilter">
      <span className="lbl">All days</span>

      <select
        className="select-field"
        value={filters.site ?? ""}
        onChange={(e) => set("site", e.target.value || null)}
      >
        <option value="">Any source</option>
        {facets?.sites.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>

      <select
        className="select-field"
        value={filters.job_type ?? ""}
        onChange={(e) => set("job_type", e.target.value || null)}
      >
        <option value="">Any job type</option>
        {facets?.job_types.map((t) => (
          <option key={t} value={t}>
            {t}
          </option>
        ))}
      </select>

      <select
        className="select-field"
        value={filters.ats ?? ""}
        onChange={(e) => set("ats", e.target.value || null)}
      >
        <option value="">Any ATS</option>
        {facets?.ats.map((a) => (
          <option key={a} value={a}>
            {a}
          </option>
        ))}
      </select>

      <span
        className={`pill${filters.eligible_only ? " on" : ""}`}
        onClick={() => set("eligible_only", !filters.eligible_only)}
      >
        Eligible
      </span>
      <span
        className={`pill${filters.above_pay_floor ? " on" : ""}`}
        onClick={() => set("above_pay_floor", !filters.above_pay_floor)}
      >
        Above pay floor
      </span>
      <span
        className={`pill${filters.unapplied_only ? " on" : ""}`}
        onClick={() => set("unapplied_only", !filters.unapplied_only)}
      >
        Not yet applied
      </span>

      <input
        className="srch"
        type="search"
        placeholder="search all days…"
        value={filters.q}
        onChange={(e) => set("q", e.target.value)}
      />
    </div>
  )
}
