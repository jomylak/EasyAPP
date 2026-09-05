import { useEffect, useMemo, useState } from "react"
import { AttentionPanel } from "@/components/AttentionPanel"
import { DayTable } from "@/components/DayTable"
import { GlobalFilterBar } from "@/components/GlobalFilterBar"
import { NumericFilter } from "@/components/NumericFilter"
import { api } from "@/lib/api"
import type { DayBucket, Facets, GlobalFilters } from "@/lib/types"
import { EMPTY_GLOBAL_FILTERS } from "@/lib/types"
import { defaultPageSize } from "@/lib/utils"

// Default window for the "needs attention" panels -- a week is a reasonable
// "haven't looked in a while" horizon; the user can widen or clear it.
const DEFAULT_ATTENTION_WINDOW_DAYS = 7

interface Props {
  selected: Set<string>
  onToggleSelect: (url: string) => void
  onToggleMany: (urls: string[], checked: boolean) => void
}

export function BrowseTab({ selected, onToggleSelect, onToggleMany }: Props) {
  const [days, setDays] = useState<DayBucket[] | null>(null)
  const [facets, setFacets] = useState<Facets | null>(null)
  const [globalFilters, setGlobalFilters] = useState<GlobalFilters>(EMPTY_GLOBAL_FILTERS)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.days().then((d) => setDays(d.days)).catch((e) => setError(String(e)))
    api.facets().then(setFacets).catch(() => {})
  }, [])

  const dayList = useMemo(() => days ?? [], [days])
  // Baseline "worth surfacing" floor for both attention panels -- a company
  // below this bar doesn't show up here even if it's the best fit around,
  // since the whole point is not missing something good, not everything.
  const ATTENTION_MIN_PRESTIGE = 6
  // null = no window, show every unapplied posting that clears the bar ever.
  const [attentionWindowDays, setAttentionWindowDays] = useState<number | null>(
    DEFAULT_ATTENTION_WINDOW_DAYS,
  )

  return (
    <>
      <GlobalFilterBar facets={facets} filters={globalFilters} onChange={setGlobalFilters} />

      <div className="attention-section">
        <div className="attention-heading">
          <span className="lbl">Priority</span>
          <NumericFilter
            id="attention-window"
            label="Posted within"
            value={attentionWindowDays}
            onChange={setAttentionWindowDays}
            unit="days"
          />
        </div>
        <div className="attention-grid">
          <AttentionPanel
            title="Top Prestige"
            defaultSort="prestige"
            minPrestige={ATTENTION_MIN_PRESTIGE}
            postedWithinDays={attentionWindowDays}
            globalFilters={globalFilters}
            selected={selected}
            onToggleSelect={onToggleSelect}
            onToggleMany={onToggleMany}
          />
          <AttentionPanel
            title="Best Fit"
            defaultSort="fit"
            minPrestige={ATTENTION_MIN_PRESTIGE}
            postedWithinDays={attentionWindowDays}
            globalFilters={globalFilters}
            selected={selected}
            onToggleSelect={onToggleSelect}
            onToggleMany={onToggleMany}
          />
        </div>
      </div>

      {error && (
        <div style={{ padding: 16, color: "var(--a-bad)" }}>Failed to load days: {error}</div>
      )}
      {!error && days === null && (
        <div style={{ padding: 16, color: "var(--a-text-3)" }}>Loading days…</div>
      )}
      {days !== null && days.length === 0 && (
        <div style={{ padding: 16, color: "var(--a-text-3)" }}>No jobs in the database yet.</div>
      )}

      <div className="daylist">
        {dayList.map((day, index) => (
          <DayTable
            key={day.day}
            day={day}
            defaultPageSize={defaultPageSize(index)}
            globalFilters={globalFilters}
            selected={selected}
            onToggleSelect={onToggleSelect}
            onToggleMany={onToggleMany}
          />
        ))}
      </div>
    </>
  )
}
