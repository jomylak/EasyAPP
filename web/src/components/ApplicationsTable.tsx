import { useEffect, useMemo, useState } from "react"
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

const STATUS_PILLS: { id: string | null; label: string }[] = [
  { id: null, label: "All" },
  { id: "applied", label: "Applied" },
  { id: "failed", label: "Failed" },
  { id: "queued", label: "Queued" },
  { id: "in_progress", label: "In progress" },
  { id: "manual", label: "Manual" },
]

type SortKey = "when" | "applied_at" | "cost" | "status" | "backend" | "company"

const SORT_LABEL: Record<SortKey, string> = {
  when: "Last attempt",
  applied_at: "Applied",
  cost: "Cost",
  status: "Status",
  backend: "Backend",
  company: "Company",
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
  const debouncedSearch = useDebounced(search, 200)

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

  function sortIndicator(key: SortKey) {
    return sortKey === key ? (sortDesc ? " ▾" : " ▴") : ""
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
            onClick={() => setStatusFilter(p.id)}
          >
            {p.label}
          </span>
        ))}
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
                <td colSpan={10} style={{ color: "var(--a-bad)", padding: "10px" }}>
                  Failed to load: {error}
                </td>
              </tr>
            )}
            {!error && visible.length === 0 && !loading && (
              <tr>
                <td colSpan={10} style={{ padding: "10px", color: "var(--a-text-3)" }}>
                  Nothing clears this filter.
                </td>
              </tr>
            )}
            {visible.map((r, i) => {
              const status = r.apply_status ?? "—"
              return (
                <tr key={r.url}>
                  <td className="idx">{i + 1}</td>
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
                  </td>
                  <td className="loc">{formatWhen(r.applied_at)}</td>
                  <td className="num">{formatCost(r.apply_cost_usd)}</td>
                  <td className="loc">{r.apply_backend ?? "—"}</td>
                  <td className="loc">{formatWhen(r.last_attempted_at)}</td>
                  <td>
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
                  <td>
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
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
