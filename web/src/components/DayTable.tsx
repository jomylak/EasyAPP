import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
  type Row,
} from "@tanstack/react-table"
import { useEffect, useMemo, useRef, useState } from "react"
import { JobExpansion } from "@/components/JobExpansion"
import { NumericFilter } from "@/components/NumericFilter"
import { api } from "@/lib/api"
import type { DayBucket, DayFilters, GlobalFilters, JobRow, SortKey } from "@/lib/types"
import { EMPTY_DAY_FILTERS } from "@/lib/types"
import { fitColor, formatDayLabel, formatDaysAgo, formatPay, preColor, useDebounced } from "@/lib/utils"

export const ROW_HEIGHT = 34
export const HEADER_HEIGHT = 34
// How many rows each infinite-scroll fetch pulls in. Independent of the box's
// visible height, which the user controls with the resize grip.
const CHUNK = 40
// Start fetching the next chunk this many px before the user hits bottom.
const PREFETCH_MARGIN = 300
const MIN_BOX_HEIGHT = 140
const MAX_BOX_HEIGHT = 900

export const SORT_LABEL: Record<SortKey, string> = {
  top: "Top",
  prestige: "Prestige",
  fit: "Fit",
  desirability: "Desirability",
  company: "Company",
  title: "Title",
  posted: "Posted",
  pay: "Pay",
}

interface HeaderSpec {
  id: string
  label: string
  sortKey?: SortKey
  numeric?: boolean
  className?: string
}

export const HEADERS: HeaderSpec[] = [
  { id: "company", label: "Company", sortKey: "company" },
  { id: "title", label: "Title", sortKey: "title" },
  { id: "location", label: "Location" },
  { id: "posted", label: "Posted", sortKey: "posted", numeric: true },
  { id: "pay", label: "Pay", sortKey: "pay" },
  { id: "prestige", label: "Pres", sortKey: "prestige", numeric: true },
  { id: "fit", label: "Fit", sortKey: "fit", numeric: true },
  { id: "desirability", label: "Des", sortKey: "desirability", numeric: true },
]

// +1 for the checkbox column, +1 for the index column.
export const COL_COUNT = HEADERS.length + 2

const columnHelper = createColumnHelper<JobRow>()

export const columns = [
  columnHelper.accessor("company", {
    id: "company",
    // Big-tech employers get the animated gold sweep. These postings are
    // pinned above the ranking rather than weighted into it, so they need to
    // be identifiable at a glance in a dense table -- the whole reason the
    // tier exists is that their fit scores are often terrible (Meta's
    // internships average 3.4) and nothing else on the row would say
    // "apply to this one anyway".
    cell: (info) => {
      const name = info.getValue()
      if (!name) return "—"
      const tier = info.row.original.company_tier
      if (!tier) return name
      return (
        <span className={tier === "tier1" ? "tier1-company" : "tier-adjacent"}>
          {name}
        </span>
      )
    },
  }),
  columnHelper.accessor("title", {
    id: "title",
    cell: (info) => info.getValue() || "—",
  }),
  columnHelper.accessor("location", {
    id: "location",
    cell: (info) => info.getValue() || "—",
  }),
  columnHelper.accessor("posted", {
    id: "posted",
    cell: (info) => {
      const v = info.getValue()
      return <span title={v ? new Date(v).toLocaleString() : undefined}>{formatDaysAgo(v)}</span>
    },
  }),
  columnHelper.display({
    id: "pay",
    cell: (info) => formatPay(info.row.original),
  }),
  columnHelper.accessor("company_prestige", {
    id: "prestige",
    cell: (info) => info.getValue() ?? "—",
  }),
  columnHelper.accessor("fit_score", {
    id: "fit",
    cell: (info) => info.getValue() ?? "—",
  }),
  columnHelper.accessor("desirability_score", {
    id: "desirability",
    cell: (info) => {
      const v = info.getValue()
      return v === null || v === undefined ? "—" : v.toFixed(1)
    },
  }),
]

interface Props {
  day: DayBucket
  defaultPageSize: number
  globalFilters: GlobalFilters
  selected: Set<string>
  onToggleSelect: (url: string, jobType: string | null, tableId: string, posted: string | null) => void
}

interface Preset {
  label: string
  filters: DayFilters
  sort: SortKey
  // Narrow this day to big-tech postings only. Not a DayFilters key because
  // it's a server-side scope, not one of the numeric bars the per-day
  // controls edit -- clearing the bars must not clear it.
  tierOnly?: boolean
}

export function DayTable({
  day,
  defaultPageSize,
  globalFilters,
  selected,
  onToggleSelect,
}: Props) {
  const [sort, setSort] = useState<SortKey>("prestige")
  const [filters, setFilters] = useState<DayFilters>(EMPTY_DAY_FILTERS)
  const [activeChip, setActiveChip] = useState<string | null>(null)
  const [openRows, setOpenRows] = useState<Set<string>>(new Set())

  // Rows accumulate across scroll-triggered fetches -- this is one flat list
  // for the whole day, not a single page. `nextPage` is the next offset to
  // request; a request generation counter guards against a late response
  // from before a filter change landing after the reset.
  const [rows, setRows] = useState<JobRow[]>([])
  const [total, setTotal] = useState(day.total)
  const [nextPage, setNextPage] = useState(0)
  const [loadingInitial, setLoadingInitial] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const requestIdRef = useRef(0)
  const fetchingRef = useRef(false)

  const wrapRef = useRef<HTMLDivElement>(null)

  // The box the user resizes by dragging the grip. Starts sized to roughly
  // this day's default row count (recency-based), capped so "today" doesn't
  // open the page already sprawling.
  const [boxHeight, setBoxHeight] = useState(() =>
    Math.min(Math.max(defaultPageSize, 4), 14) * ROW_HEIGHT + HEADER_HEIGHT,
  )

  const debouncedFilters = useDebounced(filters, 250)

  const presets: Record<string, Preset> = useMemo(
    () => ({
      prestige: {
        label: `${day.total} postings · ${day.prestige} at prestige 9+`,
        filters: { ...EMPTY_DAY_FILTERS, min_prestige: 9 },
        sort: "prestige",
      },
      fit: {
        label: `${day.total} postings · ${day.best_fit} clear the bar`,
        filters: { ...EMPTY_DAY_FILTERS, min_fit: 8, min_desirability: 6 },
        sort: "fit",
      },
      bigtech: {
        label: `${day.total} postings · big tech only`,
        // No bars at all, deliberately: closed mouths don't get fed. A
        // prestige-10 posting with a fit of 3 is exactly what this view is
        // for, and any min_* here would hide it.
        filters: EMPTY_DAY_FILTERS,
        sort: "top",
        tierOnly: true,
      },
      all: {
        label: `${day.total} postings`,
        filters: EMPTY_DAY_FILTERS,
        sort: "top",
      },
    }),
    [day.total, day.prestige, day.best_fit],
  )

  function buildQuery(pageIndex: number) {
    return {
      day: day.day,
      sort,
      page: pageIndex,
      page_size: CHUNK,
      min_fit: debouncedFilters.min_fit,
      min_desirability: debouncedFilters.min_desirability,
      min_prestige: debouncedFilters.min_prestige,
      min_pay: debouncedFilters.min_pay,
      q: debouncedFilters.q || globalFilters.q || undefined,
      job_type: globalFilters.job_type,
      site: globalFilters.site,
      ats: globalFilters.ats.length ? globalFilters.ats.join(",") : undefined,
      above_pay_floor: globalFilters.above_pay_floor,
      unapplied_only: globalFilters.unapplied_only,
      terminal_only: globalFilters.terminal_only,
      likely_terminal_only: globalFilters.likely_terminal_only,
      eligible_only: globalFilters.eligible_only,
      location: globalFilters.location,
      term: globalFilters.term,
      tier_only:
        globalFilters.tier_only ||
        (activeChip ? presets[activeChip]?.tierOnly : false) ||
        false,
      // Always on: a big-tech posting is never hidden by a threshold, in any
      // view. Without this the "Top Prestige" preset's own min_prestige 9
      // would still cut prestige-8 Datadog/IBM postings the tier is meant to
      // guarantee.
      include_tier: true,
    }
  }

  function loadPage(id: number, pageIndex: number, replace: boolean) {
    fetchingRef.current = true
    if (replace) setLoadingInitial(true)
    else setLoadingMore(true)
    api
      .jobs(buildQuery(pageIndex))
      .then((res) => {
        if (id !== requestIdRef.current) return
        setRows((prev) => (replace ? res.rows : [...prev, ...res.rows]))
        setTotal(res.total)
        setNextPage(pageIndex + 1)
        setLoadError(null)
      })
      .catch((e) => {
        if (id === requestIdRef.current) setLoadError(String(e))
      })
      .finally(() => {
        fetchingRef.current = false
        if (id !== requestIdRef.current) return
        if (replace) setLoadingInitial(false)
        else setLoadingMore(false)
      })
  }

  // Fetch this day's first chunk whenever anything that scopes the query
  // changes. Deliberately its own effect/query per DayTable instance -- each
  // day is an independent table, not a filtered slice of one global one.
  useEffect(() => {
    const id = ++requestIdRef.current
    setRows([])
    setTotal(day.total)
    setNextPage(0)
    setOpenRows(new Set())
    loadPage(id, 0, true)
    // activeChip is a dep because the Big Tech preset changes the *scope*
    // (tier_only) without touching filters or sort -- going to it from "All"
    // would otherwise leave every dep identical and never refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day.day, sort, debouncedFilters, globalFilters, activeChip])

  // Infinite scroll: fetch the next chunk when the user nears the bottom of
  // this day's own scroll box, or when the box is taller than the content
  // loaded so far (e.g. right after dragging the grip taller).
  function maybeLoadMore() {
    if (fetchingRef.current) return
    if (rows.length >= total) return
    const el = wrapRef.current
    if (!el) return
    const needsFill = el.scrollHeight <= el.clientHeight + 2
    const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - PREFETCH_MARGIN
    if (needsFill || nearBottom) {
      loadPage(requestIdRef.current, nextPage, false)
    }
  }

  useEffect(() => {
    maybeLoadMore()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows.length, total, boxHeight])

  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (r) => r.url,
    manualSorting: true,
  })

  const tableRows = table.getRowModel().rows

  function applyPreset(id: string) {
    const preset = presets[id]
    if (!preset) return
    setActiveChip(id)
    setFilters(preset.filters)
    setSort(preset.sort)
    setOpenRows(new Set())
  }

  function toggleOpen(url: string) {
    setOpenRows((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
  }

  // ---- resize grip: drag the bottom edge to grow or shrink this day's box -
  const dragState = useRef<{ startY: number; startHeight: number } | null>(null)
  function onGripDown(e: React.MouseEvent) {
    e.preventDefault() // otherwise the drag also starts a text selection, which
    // both looks broken and can steal the mouseup before our listener sees it
    window.getSelection()?.removeAllRanges()
    dragState.current = { startY: e.clientY, startHeight: boxHeight }
    const prevUserSelect = document.body.style.userSelect
    const prevCursor = document.body.style.cursor
    document.body.style.userSelect = "none"
    document.body.style.cursor = "ns-resize"
    const onMove = (ev: MouseEvent) => {
      if (!dragState.current) return
      const dy = ev.clientY - dragState.current.startY
      const next = Math.max(MIN_BOX_HEIGHT, Math.min(MAX_BOX_HEIGHT, dragState.current.startHeight + dy))
      setBoxHeight(next)
    }
    const onUp = () => {
      dragState.current = null
      document.body.style.userSelect = prevUserSelect
      document.body.style.cursor = prevCursor
      window.removeEventListener("mousemove", onMove)
      window.removeEventListener("mouseup", onUp)
    }
    window.addEventListener("mousemove", onMove)
    window.addEventListener("mouseup", onUp)
  }

  const chipLabel = activeChip ? presets[activeChip]?.label : null
  const visibleRowEstimate = Math.max(1, Math.round((boxHeight - HEADER_HEIGHT) / ROW_HEIGHT))

  return (
    <section className="day-panel">
      <div className="dayhead">
        <span className="dayname">{formatDayLabel(day.day)}</span>
        <span className="daycount">
          {chipLabel ?? `${day.total} postings`}
          {loadingInitial ? " · loading…" : ""}
        </span>
        <div className="chips">
          <button
            className={`chip${activeChip === "prestige" ? " on" : ""}`}
            onClick={() => applyPreset("prestige")}
          >
            Top Prestige<span className="c">{day.prestige}</span>
          </button>
          <button
            className={`chip${activeChip === "fit" ? " on" : ""}`}
            onClick={() => applyPreset("fit")}
          >
            Best Fit<span className="c">{day.best_fit}</span>
          </button>
          <button
            className={`chip chip-tier${activeChip === "bigtech" ? " on" : ""}`}
            onClick={() => applyPreset("bigtech")}
            title="FAANG and big tech, no score bars at all"
          >
            Big Tech
          </button>
          <button
            className={`chip${activeChip === "all" ? " on" : ""}`}
            onClick={() => applyPreset("all")}
          >
            All<span className="c">{day.total}</span>
          </button>
        </div>
      </div>

      <div className="gfilter" style={{ borderBottom: "none", paddingBottom: 4 }}>
        <span className="lbl">{formatDayLabel(day.day).split(" · ")[1] ?? day.day} only</span>
        <NumericFilter
          id={`f-pres-${day.day}`}
          label="Pres"
          value={filters.min_prestige}
          onChange={(v) => setFilters((f) => ({ ...f, min_prestige: v }))}
        />
        <NumericFilter
          id={`f-fit-${day.day}`}
          label="Fit"
          value={filters.min_fit}
          onChange={(v) => setFilters((f) => ({ ...f, min_fit: v }))}
        />
        <NumericFilter
          id={`f-des-${day.day}`}
          label="Des"
          value={filters.min_desirability}
          onChange={(v) => setFilters((f) => ({ ...f, min_desirability: v }))}
          step={0.5}
        />
        <NumericFilter
          id={`f-pay-${day.day}`}
          label="Pay"
          value={filters.min_pay}
          onChange={(v) => setFilters((f) => ({ ...f, min_pay: v }))}
          step={5}
          unit="$/hr"
        />
        <input
          className="srch"
          type="search"
          placeholder="title, company, keyword"
          value={filters.q}
          onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
        />
        <button
          className="chip"
          onClick={() => {
            setFilters(EMPTY_DAY_FILTERS)
            setActiveChip(null)
          }}
        >
          Clear
        </button>
      </div>

      <div className="tablewrap" style={{ maxHeight: boxHeight }} ref={wrapRef} onScroll={maybeLoadMore}>
        <table>
          <thead>
            <tr>
              <th className="cbcell" />
              <th className="col-idx num">#</th>
              {HEADERS.map((h) => (
                <th
                  key={h.id}
                  className={["col-" + h.id, h.numeric ? "num" : "", h.sortKey ? "sortable" : ""].join(" ").trim()}
                  title={h.sortKey ? `Sort by ${SORT_LABEL[h.sortKey]}` : undefined}
                  onClick={h.sortKey ? () => setSort(h.sortKey as SortKey) : undefined}
                >
                  {h.label}
                  {h.sortKey === sort ? " ▾" : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loadError && (
              <tr>
                <td colSpan={COL_COUNT} style={{ color: "var(--a-bad)", padding: "10px" }}>
                  Failed to load: {loadError}
                </td>
              </tr>
            )}
            {!loadError && tableRows.length === 0 && !loadingInitial && (
              <tr>
                <td colSpan={COL_COUNT} style={{ padding: "10px", color: "var(--a-text-3)" }}>
                  Nothing clears these filters.
                </td>
              </tr>
            )}
            {tableRows.map((row) => {
              const r = row.original
              const isSel = selected.has(r.url)
              const isOpen = openRows.has(r.url)
              return (
                <RowPair
                  key={r.url}
                  row={row}
                  original={r}
                  isSel={isSel}
                  isOpen={isOpen}
                  tableId={day.day}
                  onToggleSelect={onToggleSelect}
                  onToggleOpen={toggleOpen}
                />
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="foot">
        <span>
          showing {tableRows.length} of {total}
          {loadingMore ? " · loading more…" : ""}
        </span>
        <span>~{visibleRowEstimate} rows visible · drag to resize ↕</span>
      </div>
      <div className="grip" title="Drag to grow or shrink this day's box" onMouseDown={onGripDown} />
    </section>
  )
}

export interface RowPairProps {
  row: Row<JobRow>
  original: JobRow
  isSel: boolean
  isOpen: boolean
  tableId: string
  onToggleSelect: (url: string, jobType: string | null, tableId: string, posted: string | null) => void
  onToggleOpen: (url: string) => void
}

/**
 * Grad-date-safety signal for internships, at a glance -- see
 * compute_terminal_internships / compute_likely_terminal_internships /
 * compute_remote_spring_internships in scoring/scorer.py for how each is
 * derived. Confirmed and remote-spring share the apply queue's top-priority
 * tier; likely is a manual-review signal only, never queue-boosted.
 */
function TerminalBadge({ row }: { row: JobRow }) {
  if (row.is_terminal_internship === "yes") {
    return (
      <span className="tbadge tbadge-confirmed" title="Confirmed terminal: posting explicitly welcomes an already-graduated candidate">
        T
      </span>
    )
  }
  if (row.is_remote_spring_internship === "yes") {
    return (
      <span className="tbadge tbadge-confirmed" title="Remote Spring internship: term ends before graduation, no grad-date conflict possible">
        RS
      </span>
    )
  }
  if (row.is_terminal_internship_likely === "yes") {
    // terminal_evidence_hint is a review-aid label only (never affects
    // ranking/filtering, see compute_terminal_evidence_hints in
    // scoring/scorer.py) -- "silent" means the posting never mentions
    // grad timing/enrollment at all; "mentions_enrollment" means it
    // describes a "currently pursuing"/"currently enrolled" candidate
    // profile without stating that as a requirement, which the scoring
    // prompt deliberately treats as non-disqualifying but which reads
    // less clean-cut than true silence to a human skimming this list.
    const hint =
      row.terminal_evidence_hint === "mentions_enrollment"
        ? "Likely terminal: strong match; posting mentions \"currently pursuing/enrolled\" language (boilerplate candidate description, not a stated requirement) but never explicitly says either way about post-grad eligibility -- worth a manual look"
        : "Likely terminal: strong match; posting says nothing at all about grad timing or enrollment -- worth a manual look"
    return (
      <span className="tbadge tbadge-likely" title={hint}>
        L?
      </span>
    )
  }
  return null
}

export function RowPair({ row, original, isSel, isOpen, tableId, onToggleSelect, onToggleOpen }: RowPairProps) {
  const cellsById = new Map(row.getVisibleCells().map((c) => [c.column.id, c]))
  return (
    <>
      <tr className={`row${isSel ? " sel" : ""}`} onClick={() => onToggleOpen(original.url)}>
        <td className="cbcell">
          <input
            type="checkbox"
            checked={isSel}
            aria-label={`Select ${original.company ?? original.title ?? original.url}`}
            onClick={(e) => e.stopPropagation()}
            onChange={() => {
              onToggleSelect(original.url, original.job_type, tableId, original.posted)
              // Checking a row is a confirm-and-move-on action -- close the
              // expanded detail so the next row is ready to review. Only on
              // check, not uncheck: un-ticking usually means "wait, let me
              // look again," so leave it open.
              if (!isSel && isOpen) onToggleOpen(original.url)
            }}
          />
        </td>
        <td className="idx">{row.index + 1}</td>
        <td className="co" title={original.company ?? undefined}>{flexRender(cellsById.get("company")!.column.columnDef.cell, cellsById.get("company")!.getContext())}</td>
        <td className="ti" title={original.title ?? undefined}>
          <TerminalBadge row={original} />
          {flexRender(cellsById.get("title")!.column.columnDef.cell, cellsById.get("title")!.getContext())}
        </td>
        <td className="loc">
          {flexRender(cellsById.get("location")!.column.columnDef.cell, cellsById.get("location")!.getContext())}
        </td>
        <td className="num posted">
          {flexRender(cellsById.get("posted")!.column.columnDef.cell, cellsById.get("posted")!.getContext())}
        </td>
        <td className="pay">{flexRender(cellsById.get("pay")!.column.columnDef.cell, cellsById.get("pay")!.getContext())}</td>
        <td className="num" style={{ color: preColor(original.company_prestige) }}>
          {flexRender(cellsById.get("prestige")!.column.columnDef.cell, cellsById.get("prestige")!.getContext())}
        </td>
        <td className="num" style={{ color: fitColor(original.fit_score) }}>
          {flexRender(cellsById.get("fit")!.column.columnDef.cell, cellsById.get("fit")!.getContext())}
        </td>
        <td className="num">
          {flexRender(cellsById.get("desirability")!.column.columnDef.cell, cellsById.get("desirability")!.getContext())}
        </td>
      </tr>
      <tr className={`exp${isOpen ? " open" : ""}`}>
        <td colSpan={COL_COUNT}>
          <div className="slide">
            <div>
              <JobExpansion row={original} open={isOpen} />
            </div>
          </div>
        </td>
      </tr>
    </>
  )
}
