import { getCoreRowModel, useReactTable } from "@tanstack/react-table"
import { useEffect, useRef, useState } from "react"
import { COL_COUNT, columns, HEADERS, HEADER_HEIGHT, ROW_HEIGHT, RowPair, SORT_LABEL } from "@/components/DayTable"
import { api } from "@/lib/api"
import type { GlobalFilters, JobRow, SortKey } from "@/lib/types"

const CHUNK = 40
const PREFETCH_MARGIN = 300
export const ATTENTION_MIN_BOX_HEIGHT = 140
export const ATTENTION_MAX_BOX_HEIGHT = 900
export const ATTENTION_DEFAULT_BOX_HEIGHT = 8 * ROW_HEIGHT + HEADER_HEIGHT

interface Props {
  title: string
  defaultSort: SortKey
  minPrestige: number
  // This lane's own pay floor, in $/hr. Both bars are waived for a
  // tier1/adjacent posting -- see include_tier below.
  minPay: number
  // Fixed for this panel, independent of globalFilters.job_type -- the
  // whole point of these two panels is to BE the internship/new_grad split
  // (see BrowseTab), so each panel's type can't be knocked out by whatever
  // the global job-type dropdown happens to be set to. null = either type.
  jobType: string | null
  postedWithinDays: number | null
  globalFilters: GlobalFilters
  selected: Set<string>
  onToggleSelect: (url: string, jobType: string | null) => void
  onToggleMany: (rows: { url: string; job_type: string | null }[], checked: boolean) => void
  // Both Priority panels share one height: dragging either grip resizes both,
  // and each independently loads enough of its own rows to fill it (rather
  // than one panel loading rows while the other just stretches to match with
  // nothing to show) -- that's the whole point of sharing this instead of
  // each panel owning its own boxHeight.
  boxHeight: number
  onBoxHeightChange: (h: number) => void
}

/**
 * A cross-day "don't miss this" panel -- the same job can (and often does)
 * appear in both the prestige and fit panels, and in whatever day table it
 * came from. There's no dedup logic here on purpose: `selected` is the one
 * Set<url> App.tsx owns for the whole tab, so checking a job anywhere marks
 * it checked everywhere it's shown, for free.
 *
 * Unbounded by row count -- unlike the old top-50 cut, this shows everything
 * that clears the prestige floor within the posted-date window, and scrolls
 * (infinite scroll, same pattern as DayTable) rather than truncating.
 */
export function AttentionPanel({
  title,
  defaultSort,
  minPrestige,
  minPay,
  jobType,
  postedWithinDays,
  globalFilters,
  selected,
  onToggleSelect,
  onToggleMany,
  boxHeight,
  onBoxHeightChange,
}: Props) {
  const [sort, setSort] = useState<SortKey>(defaultSort)
  const [rows, setRows] = useState<JobRow[]>([])
  const [total, setTotal] = useState(0)
  const [nextPage, setNextPage] = useState(0)
  const [loadingInitial, setLoadingInitial] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const requestIdRef = useRef(0)
  const fetchingRef = useRef(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [openRows, setOpenRows] = useState<Set<string>>(new Set())

  function buildQuery(pageIndex: number) {
    return {
      sort,
      page: pageIndex,
      page_size: CHUNK,
      min_prestige: minPrestige,
      min_pay: minPay,
      unapplied_only: true,
      posted_within_days: postedWithinDays,
      q: globalFilters.q || undefined,
      job_type: jobType,
      site: globalFilters.site,
      ats: globalFilters.ats.length ? globalFilters.ats.join(",") : undefined,
      above_pay_floor: globalFilters.above_pay_floor,
      terminal_only: globalFilters.terminal_only,
      likely_terminal_only: globalFilters.likely_terminal_only,
      location: globalFilters.location,
      term: globalFilters.term,
      tier_only: globalFilters.tier_only,
      // The point of the whole panel is not missing a good opening, and the
      // bars above are exactly what used to hide the best ones -- Meta's
      // prestige-10 internships average a fit of 3.4, and 128 of the 470
      // eligible high-prestige internships state no salary at all. A
      // big-tech posting clears every bar here by fiat.
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

  useEffect(() => {
    const id = ++requestIdRef.current
    setRows([])
    setTotal(0)
    setNextPage(0)
    setOpenRows(new Set())
    loadPage(id, 0, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, minPrestige, minPay, jobType, postedWithinDays, globalFilters])

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
  const allLoadedSelected = tableRows.length > 0 && tableRows.every((r) => selected.has(r.original.url))

  function toggleOpen(url: string) {
    setOpenRows((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
  }

  // ---- resize grip: same drag-to-grow-or-shrink mechanic as DayTable ------
  const dragState = useRef<{ startY: number; startHeight: number } | null>(null)
  function onGripDown(e: React.MouseEvent) {
    e.preventDefault()
    window.getSelection()?.removeAllRanges()
    dragState.current = { startY: e.clientY, startHeight: boxHeight }
    const prevUserSelect = document.body.style.userSelect
    const prevCursor = document.body.style.cursor
    document.body.style.userSelect = "none"
    document.body.style.cursor = "ns-resize"
    const onMove = (ev: MouseEvent) => {
      if (!dragState.current) return
      const dy = ev.clientY - dragState.current.startY
      const next = Math.max(
        ATTENTION_MIN_BOX_HEIGHT,
        Math.min(ATTENTION_MAX_BOX_HEIGHT, dragState.current.startHeight + dy),
      )
      onBoxHeightChange(next)
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

  return (
    <section className="day-panel">
      <div className="dayhead">
        <span className="dayname">{title}</span>
        <span className="daycount">
          {jobType ? `${jobType} · ` : ""}unapplied · prestige {minPrestige}+ ·{" "}
          {jobType === "new_grad"
            ? `$${Math.round(minPay * 2080 / 1000)}k+`
            : `$${minPay}/hr+`}
          {postedWithinDays ? ` · last ${postedWithinDays}d` : ""} · big tech always ·{" "}
          {total} total
          {loadingInitial ? " · loading…" : ""}
        </span>
      </div>

      <div className="tablewrap" style={{ maxHeight: boxHeight }} ref={wrapRef} onScroll={maybeLoadMore}>
        <table>
          <thead>
            <tr>
              <th className="cbcell">
                <input
                  type="checkbox"
                  aria-label="Select all loaded"
                  checked={allLoadedSelected}
                  onChange={(e) =>
                    onToggleMany(
                      tableRows.map((r) => ({
                        url: r.original.url,
                        job_type: r.original.job_type,
                      })),
                      e.target.checked,
                    )
                  }
                />
              </th>
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
                  Nothing clears this bar right now.
                </td>
              </tr>
            )}
            {tableRows.map((row) => {
              const r = row.original
              return (
                <RowPair
                  key={r.url}
                  row={row}
                  original={r}
                  isSel={selected.has(r.url)}
                  isOpen={openRows.has(r.url)}
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
      </div>
      <div className="grip" title="Drag to grow or shrink this panel" onMouseDown={onGripDown} />
    </section>
  )
}
