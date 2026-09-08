import { useEffect, useMemo, useState } from "react"
import { ATTENTION_DEFAULT_BOX_HEIGHT, AttentionPanel } from "@/components/AttentionPanel"
import { DayTable } from "@/components/DayTable"
import { GlobalFilterBar } from "@/components/GlobalFilterBar"
import { NumericFilter } from "@/components/NumericFilter"
import { SplitCounter } from "@/components/SplitCounter"
import { api } from "@/lib/api"
import type { DayBucket, Facets, GlobalFilters } from "@/lib/types"
import { EMPTY_GLOBAL_FILTERS } from "@/lib/types"
import { defaultPageSize } from "@/lib/utils"

// Default window for the "needs attention" panels -- a week is a reasonable
// "haven't looked in a while" horizon; the user can widen or clear it.
const DEFAULT_ATTENTION_WINDOW_DAYS = 7

interface Props {
  selected: Set<string>
  selectedTypes: Map<string, string | null>
  onToggleSelect: (url: string, jobType: string | null) => void
  onToggleMany: (rows: { url: string; job_type: string | null }[], checked: boolean) => void
}

export function BrowseTab({ selected, selectedTypes, onToggleSelect, onToggleMany }: Props) {
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
  // Per-lane pay floors, in $/hr because that's the one unit both lanes can
  // be compared in (parse_pay_range normalises annual salaries down to it).
  // $43.27/hr is $90,000/yr -- the new-grad target -- and $30/hr is the
  // internship floor straight off the profile. Editable per panel; a
  // big-tech posting is exempt from both regardless (include_tier).
  const NEW_GRAD_PAY_FLOOR = 43.27
  const INTERNSHIP_PAY_FLOOR = 30
  // null = no window, show every unapplied posting that clears the bar ever.
  const [attentionWindowDays, setAttentionWindowDays] = useState<number | null>(
    DEFAULT_ATTENTION_WINDOW_DAYS,
  )
  // Shared by both Priority panels -- dragging either grip resizes both to
  // the same level, rather than one growing with real rows while the other
  // just stretches to match with nothing loaded to fill it.
  const [attentionBoxHeight, setAttentionBoxHeight] = useState(ATTENTION_DEFAULT_BOX_HEIGHT)

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
          <SplitCounter selectedTypes={selectedTypes} />
        </div>
        <div className="attention-grid">
          {/* Fixed to job_type on purpose -- this IS the internship/new_grad
              split the weekly selection process works from, so it can't be
              knocked out by the global job-type dropdown. The SplitCounter
              above reads the 60/40 ratio off whatever is ticked across both.

              Sorted "top" in both, which pins big-tech postings above the
              ranking and demotes skills fit to a tiebreaker: fit was the
              dominant term and it buried exactly these employers (Meta's
              internships average a fit of 3.4 at prestige 10). Each lane
              carries its own pay floor -- $90k/yr for new grad, $30/hr for
              internships -- and a big-tech posting is exempt from both, so
              one with no stated salary still shows. Within Internships the
              T/RS/L? badges (DayTable's TerminalBadge) flag which ones are
              grad-date-safe, so "best internships, terminal ones especially"
              stays a read-the-badges-top-down exercise on one list. */}
          <AttentionPanel
            title="Top Internships"
            defaultSort="top"
            minPrestige={ATTENTION_MIN_PRESTIGE}
            minPay={INTERNSHIP_PAY_FLOOR}
            jobType="internship"
            postedWithinDays={attentionWindowDays}
            globalFilters={globalFilters}
            selected={selected}
            onToggleSelect={onToggleSelect}
            onToggleMany={onToggleMany}
            boxHeight={attentionBoxHeight}
            onBoxHeightChange={setAttentionBoxHeight}
          />
          <AttentionPanel
            title="Top New Grad"
            defaultSort="top"
            minPrestige={ATTENTION_MIN_PRESTIGE}
            minPay={NEW_GRAD_PAY_FLOOR}
            jobType="new_grad"
            postedWithinDays={attentionWindowDays}
            globalFilters={globalFilters}
            selected={selected}
            onToggleSelect={onToggleSelect}
            onToggleMany={onToggleMany}
            boxHeight={attentionBoxHeight}
            onBoxHeightChange={setAttentionBoxHeight}
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
