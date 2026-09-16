import { Fragment, useEffect, useMemo, useState } from "react"
import { JobExpansion } from "@/components/JobExpansion"
import { api } from "@/lib/api"
import type { ApplicationRow } from "@/lib/types"
import { useDebounced } from "@/lib/utils"

const STATUS_LABEL: Record<string, string> = {
  applied: "Applied",
  failed: "Failed",
  queued: "Queued",
  in_progress: "In progress",
  manual: "Manual",
}

const POST_APPLY_LABEL: Record<string, string> = {
  none: "No response",
  oa: "OA",
  interview: "Interview",
  rejected: "Rejected",
  offer: "Offer",
}
const POST_APPLY_OPTIONS = ["none", "oa", "interview", "rejected", "offer"]

const STATUS_PILLS: { id: string | null; label: string }[] = [
  { id: null, label: "All" },
  { id: "applied", label: "Applied" },
  { id: "failed", label: "Failed" },
  { id: "queued", label: "Queued" },
  { id: "in_progress", label: "In progress" },
  { id: "manual", label: "Manual" },
]

type SortKey = "when" | "applied_at" | "cost" | "status" | "backend" | "company" | "queue"

const SORT_LABEL: Record<SortKey, string> = {
  when: "Last attempt",
  applied_at: "Applied",
  cost: "Cost",
  status: "Status",
  backend: "Backend",
  company: "Company",
  queue: "Priority",
}

function formatCost(v: number | null): string {
  if (v === null || v === undefined) return "—"
  return `$${v.toFixed(2)}`
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
}

function sortValue(r: ApplicationRow, key: SortKey): string | number {
  switch (key) {
    case "when":
      return r.last_attempted_at ?? ""
    case "applied_at":
      return r.applied_at ?? ""
    case "cost":
      return r.apply_cost_usd ?? -1
    case "status":
      return r.apply_status ?? ""
    case "backend":
      return r.apply_backend ?? ""
    case "company":
      return (r.company ?? "").toLowerCase()
    case "queue":
      return r.queue_position ?? Number.MAX_SAFE_INTEGER
  }
}

/**
 * Everything with an apply outcome: applied, failed, queued, in-flight, or
 * manual. `/api/applications` has no pagination -- it's a single indexed
 * query capped at `limit` -- so this fetches once per status filter (polling
 * while a run is live) rather than the day tables' infinite scroll; sorting
 * and the search box are client-side over that one page.
 */
export function ApplicationsTable({ live }: { live: boolean }) {
  const [rows, setRows] = useState<ApplicationRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyUrl, setBusyUrl] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [sortKey, setSortKey] = useState<SortKey>("when")
  const [sortDesc, setSortDesc] = useState(true)
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null)
  // Which row's detail (JobExpansion) is open -- click-to-expand, same as
  // Browse's DayTable, so this table isn't the one place in the app a row
  // doesn't expand.
  const [openRows, setOpenRows] = useState<Set<string>>(new Set())
  // Inline "Report ineligible" note field -- which row (if any) has it open,
  // the text typed into it, and the most recent result to show back.
  const [reportingUrl, setReportingUrl] = useState<string | null>(null)
  const [reportNote, setReportNote] = useState("")
  const [reportResult, setReportResult] = useState<{ url: string; text: string } | null>(null)
  const debouncedSearch = useDebounced(search, 200)

  // Dragging only makes sense over the exact set the reorder call will
  // persist against -- the full queued list, in priority order, with
  // nothing hidden by a search filter.
  const isQueueView = statusFilter === "queued" && sortKey === "queue" && !debouncedSearch.trim()

  function refresh() {
    setLoading(true)
    api
      .applications(statusFilter, 500)
      .then((res) => {
        setRows(res.rows)
        setError(null)
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter])

  // Poll while a run is actually in flight -- otherwise the table only
  // changes in response to an action taken here, which already refreshes.
  useEffect(() => {
    if (!live) return
    const id = setInterval(refresh, 3000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, statusFilter])

  const visible = useMemo(() => {
    const q = debouncedSearch.trim().toLowerCase()
    let out = rows
    if (q) {
      out = out.filter(
        (r) => (r.company ?? "").toLowerCase().includes(q) || (r.title ?? "").toLowerCase().includes(q),
      )
    }
    out = [...out].sort((a, b) => {
      const av = sortValue(a, sortKey)
      const bv = sortValue(b, sortKey)
      const cmp = av < bv ? -1 : av > bv ? 1 : 0
      return sortDesc ? -cmp : cmp
    })
    return out
  }, [rows, debouncedSearch, sortKey, sortDesc])

  function setSort(key: SortKey) {
    if (key === sortKey) setSortDesc((d) => !d)
    else {
      setSortKey(key)
      setSortDesc(true)
    }
  }

  async function act(r: ApplicationRow, action: "cancel" | "stop" | "retry") {
    setBusyUrl(r.url)
    try {
      if (action === "cancel") {
        // A queued row got here either fresh (never had an outcome, so
        // cancelling clears it entirely) or via Retry on a job that had
        // already failed once -- cancelling that should put it back to
        // 'failed' rather than wiping the fact it ever ran, which would
        // otherwise just look like the job vanished.
        await api.unqueue([r.url], (r.apply_attempts ?? 0) > 0 ? "failed" : undefined)
      } else if (action === "stop") {
        await api.stop(r.url)
      } else {
        await api.queue([r.url])
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyUrl(null)
      refresh()
    }
  }

  // For jobs already applied to: the pipeline has no visibility into a
  // rejection email citing graduation date, so this is how that finding gets
  // back in -- feeds the same company-wide sibling sweep the automated
  // grad_date_mismatch detector uses (see apply.ineligibility), not just a
  // flag on this one row. An inline note field, not window.prompt -- a
  // native dialog blocks the page until dismissed, and there's no reason to
  // make this the one action in the app that works differently.
  async function reportIneligible(r: ApplicationRow) {
    setBusyUrl(r.url)
    try {
      const res = await api.reportIneligible(r.url, reportNote)
      const swept = res.siblings_softened + res.siblings_disqualified
      setReportResult({
        url: r.url,
        text:
          swept > 0
            ? `Marked ineligible. Also ${res.siblings_disqualified ? "disqualified" : "flagged for review"} ` +
              `${swept} sibling posting(s) at ${res.company ?? "this company"}.`
            : "Marked ineligible.",
      })
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyUrl(null)
      setReportingUrl(null)
      setReportNote("")
      refresh()
    }
  }

  async function setPostApplyStatus(r: ApplicationRow, status: string) {
    setBusyUrl(r.url)
    try {
      await api.setPostApplyStatus(r.url, status)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyUrl(null)
      refresh()
    }
  }

  function toggleOpen(url: string) {
    setOpenRows((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
  }

  function sortIndicator(key: SortKey) {
    return sortKey === key ? (sortDesc ? " ▾" : " ▴") : ""
  }

  function selectQueuedPill() {
    setStatusFilter("queued")
    setSortKey("queue")
    setSortDesc(false)
  }

  // Drag-reorder writes queue_position onto local state immediately (so the
  // row doesn't snap back to its old spot before the request round-trips)
  // and persists in the background via the same call that folds every
  // queued job into one fresh, contiguously-numbered batch.
  function handleDrop(targetIndex: number) {
    if (draggedIndex === null || draggedIndex === targetIndex) {
      setDraggedIndex(null)
      return
    }
    const arr = [...visible]
    const [moved] = arr.splice(draggedIndex, 1)
    arr.splice(targetIndex, 0, moved)
    const reindexed = arr.map((r, i) => ({ ...r, queue_position: i }))
    setRows(reindexed)
    setDraggedIndex(null)
    api.reorderQueue(reindexed.map((r) => r.url)).catch((e) => setError(String(e)))
  }

  return (
    <section className="day-panel">
      <div className="dayhead">
        <span className="dayname">Applications</span>
        <span className="daycount">
          {visible.length} of {rows.length} rows{loading ? " · loading…" : ""}
        </span>
      </div>

      <div className="gfilter" style={{ borderBottom: "none", paddingBottom: 4 }}>
        {STATUS_PILLS.map((p) => (
          <span
            key={p.label}
            className={`pill${statusFilter === p.id ? " on" : ""}`}
            onClick={() => (p.id === "queued" ? selectQueuedPill() : setStatusFilter(p.id))}
          >
            {p.label}
          </span>
        ))}
        {isQueueView && (
          <span style={{ fontSize: 11.5, color: "var(--a-text-3)", marginLeft: 4 }}>
            drag rows to reprioritize
          </span>
        )}
        <input
          className="srch"
          type="search"
          placeholder="company, title"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <div className="tablewrap" style={{ maxHeight: 520 }}>
        <table className="apptable">
          <thead>
            <tr>
              <th className="col-idx num">#</th>
              <th className="col-company sortable" onClick={() => setSort("company")} title={`Sort by ${SORT_LABEL.company}`}>
                Company{sortIndicator("company")}
              </th>
              <th className="col-title">Title</th>
              <th className="col-status sortable" onClick={() => setSort("status")} title={`Sort by ${SORT_LABEL.status}`}>
                Status{sortIndicator("status")}
              </th>
              <th className="col-when sortable" onClick={() => setSort("applied_at")} title={`Sort by ${SORT_LABEL.applied_at}`}>
                Applied{sortIndicator("applied_at")}
              </th>
              <th className="col-status">Outcome</th>
              <th className="col-cost num sortable" onClick={() => setSort("cost")} title={`Sort by ${SORT_LABEL.cost}`}>
                Cost{sortIndicator("cost")}
              </th>
              <th className="col-backend sortable" onClick={() => setSort("backend")} title={`Sort by ${SORT_LABEL.backend}`}>
                Backend{sortIndicator("backend")}
              </th>
              <th className="col-when sortable" onClick={() => setSort("when")} title={`Sort by ${SORT_LABEL.when}`}>
                Last attempt{sortIndicator("when")}
              </th>
              <th className="col-action">Resume</th>
              <th className="col-action">Action</th>
            </tr>
          </thead>
          <tbody>
            {error && (
              <tr>
                <td colSpan={11} style={{ color: "var(--a-bad)", padding: "10px" }}>
                  Failed to load: {error}
                </td>
              </tr>
            )}
            {!error && visible.length === 0 && !loading && (
              <tr>
                <td colSpan={11} style={{ padding: "10px", color: "var(--a-text-3)" }}>
                  Nothing clears this filter.
                </td>
              </tr>
            )}
            {visible.map((r, i) => {
              const status = r.apply_status ?? "—"
              return (
                <Fragment key={r.url}>
                <tr
                  className={`row${isQueueView ? " draggable-row" : ""}`}
                  draggable={isQueueView}
                  onDragStart={isQueueView ? () => setDraggedIndex(i) : undefined}
                  onDragOver={isQueueView ? (e) => e.preventDefault() : undefined}
                  onDrop={isQueueView ? () => handleDrop(i) : undefined}
                  onClick={() => !isQueueView && toggleOpen(r.url)}
                >
                  <td className="idx" style={isQueueView ? { cursor: "grab" } : undefined}>
                    {isQueueView ? `⠿ ${i + 1}` : i + 1}
                  </td>
                  <td className="co" title={r.company ?? undefined}>
                    {r.company ? (
                      <span className={r.company_tier === "tier1" ? "tier1-company" : r.company_tier ? "tier-adjacent" : undefined}>
                        {r.company}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="ti" title={r.title ?? undefined}>{r.title || "—"}</td>
                  <td>
                    <span className={`badge ${status}`}>{STATUS_LABEL[status] ?? status}</span>
                    {status === "failed" && r.apply_error && (
                      <div
                        title={r.apply_error}
                        style={{
                          fontSize: 11,
                          color: "var(--a-text-3)",
                          marginTop: 2,
                          maxWidth: 220,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {r.apply_error}
                      </div>
                    )}
                  </td>
                  <td className="loc">{formatWhen(r.applied_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {status === "applied" || status === "manual" ? (
                      <select
                        className={`badge ${r.post_apply_status ?? "none"}`}
                        style={{ border: "none", cursor: "pointer" }}
                        value={r.post_apply_status ?? "none"}
                        disabled={busyUrl === r.url}
                        title={r.post_apply_evidence ?? undefined}
                        onChange={(e) => setPostApplyStatus(r, e.target.value)}
                      >
                        {POST_APPLY_OPTIONS.map((opt) => (
                          <option key={opt} value={opt}>
                            {POST_APPLY_LABEL[opt]}
                          </option>
                        ))}
                      </select>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="num">{formatCost(r.apply_cost_usd)}</td>
                  <td className="loc">{r.apply_backend ?? "—"}</td>
                  <td className="loc">{formatWhen(r.last_attempted_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {r.tailored_resume_path ? (
                      <a
                        href={`/api/resume?url=${encodeURIComponent(r.url)}`}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: "var(--a-sel)" }}
                      >
                        PDF
                      </a>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {status === "queued" && (
                      <button className="rowbtn danger" disabled={busyUrl === r.url} onClick={() => act(r, "cancel")}>
                        Cancel
                      </button>
                    )}
                    {status === "in_progress" && (
                      <button className="rowbtn danger" disabled={busyUrl === r.url} onClick={() => act(r, "stop")}>
                        Stop
                      </button>
                    )}
                    {status === "failed" && (
                      <button className="rowbtn" disabled={busyUrl === r.url} onClick={() => act(r, "retry")}>
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
                <tr className={`exp${openRows.has(r.url) ? " open" : ""}`}>
                  <td colSpan={11}>
                    <div className="slide">
                      <div>
                        <JobExpansion row={r} open={openRows.has(r.url)} />
                        {status === "applied" && (
                          <div style={{ marginTop: 14, borderTop: "1px solid var(--a-line)", paddingTop: 12 }}>
                            {reportingUrl === r.url ? (
                              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                <span style={{ fontSize: 12, color: "var(--a-text-2)" }}>
                                  Optional note (e.g. what the email said):
                                </span>
                                <input
                                  className="srch"
                                  style={{ flex: 1, width: "auto" }}
                                  autoFocus
                                  value={reportNote}
                                  onChange={(e) => setReportNote(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") reportIneligible(r)
                                    if (e.key === "Escape") setReportingUrl(null)
                                  }}
                                />
                                <button className="rowbtn danger" disabled={busyUrl === r.url} onClick={() => reportIneligible(r)}>
                                  {busyUrl === r.url ? "Reporting…" : "Confirm"}
                                </button>
                                <button className="rowbtn" onClick={() => setReportingUrl(null)}>
                                  Cancel
                                </button>
                              </div>
                            ) : (
                              <button
                                className="rowbtn danger"
                                disabled={busyUrl === r.url}
                                onClick={() => {
                                  setReportingUrl(r.url)
                                  setReportNote("")
                                  setReportResult(null)
                                }}
                              >
                                Report ineligible
                              </button>
                            )}
                            {reportResult && reportResult.url === r.url && (
                              <div style={{ marginTop: 8, fontSize: 11.5, color: "var(--a-text-2)" }}>
                                {reportResult.text}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                </tr>
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
