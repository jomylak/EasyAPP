import { AtsFilterDropdown } from "@/components/AtsFilterDropdown"
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
 *
 * No "Eligible" or "Summer only" pill on purpose: those used to be opt-in
 * toggles here, but there's no reason this table should ever surface a job
 * you can't honestly take or a term (Fall/Winter) you can't work -- both are
 * enforced unconditionally server-side now (queries._filter_clauses), not
 * something to remember to turn on. The Term select here narrows *within*
 * the two workable seasons; it can't widen past them.
 *
 * Location is here rather than per-day because it's a standing preference,
 * not something you'd re-decide for Tuesday. Its buckets are exactly the
 * tiers scorer._location_desirability scores against, so the filter and the
 * ranking can never disagree about what counts as New York.
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
        value={filters.location ?? ""}
        onChange={(e) => set("location", e.target.value || null)}
        title="Buckets match the location tiers the desirability score itself uses"
      >
        <option value="">Any location</option>
        <option value="nyc">New York</option>
        <option value="metro">Preferred metros</option>
        <option value="remote">Remote</option>
      </select>

      <select
        className="select-field"
        value={filters.term ?? ""}
        onChange={(e) => set("term", e.target.value || null)}
      >
        <option value="">Any term</option>
        <option value="spring">Spring</option>
        <option value="summer">Summer</option>
      </select>

      <AtsFilterDropdown
        options={facets?.ats ?? []}
        selected={filters.ats}
        onChange={(next) => set("ats", next)}
      />

      <span
        className={`pill tier${filters.tier_only ? " on" : ""}`}
        onClick={() => set("tier_only", !filters.tier_only)}
        title="FAANG, big tech, and the companies just outside it -- these get applied to regardless of how the fit scores"
      >
        Big tech only
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
      <span
        className={`pill${filters.terminal_only ? " on" : ""}`}
        onClick={() => set("terminal_only", !filters.terminal_only)}
        title="Confirmed terminal: posting explicitly welcomes an already-graduated candidate"
      >
        Confirmed terminal
      </span>
      <span
        className={`pill${filters.likely_terminal_only ? " on" : ""}`}
        onClick={() => set("likely_terminal_only", !filters.likely_terminal_only)}
        title="Strong matches whose posting never says either way about post-grad eligibility -- worth a manual look"
      >
        Likely-terminal review
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
