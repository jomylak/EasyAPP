import { useEffect, useState } from "react"
import { LaunchBar } from "@/components/LaunchBar"
import { api } from "@/lib/api"
import { useRunStream } from "@/lib/useRunStream"
import { BrowseTab } from "@/tabs/BrowseTab"
import { DashboardTab } from "@/tabs/DashboardTab"
import { SettingsTab } from "@/tabs/SettingsTab"

type Tab = "browse" | "dashboard" | "settings"

type SelectionEntry = { url: string; tableId: string; posted: string | null }

// Selecting jobs in Browse is real work -- losing it to a page reload (a
// pushed frontend update, an accidental refresh) has no upside, so the raw
// click sequence is mirrored into localStorage and everything else
// (selected Set, selectedTypes Map) is rebuilt from it on load. Per-browser
// only, which is fine here: this app has exactly one user.
const SELECTION_KEY = "applypilot.selectionOrder"

function loadSelectionOrder(): SelectionEntry[] {
  try {
    const raw = localStorage.getItem(SELECTION_KEY)
    return raw ? (JSON.parse(raw) as SelectionEntry[]) : []
  } catch {
    return []
  }
}

export default function App() {
  const [tab, setTab] = useState<Tab>("browse")
  const initialOrder = loadSelectionOrder()
  // A Set of urls held above the day tables, so it survives across days and
  // tab switches and drives the sticky launch bar.
  const [selected, setSelected] = useState<Set<string>>(new Set(initialOrder.map((e) => e.url)))
  // url -> job_type for everything currently ticked, so the 60/40 split
  // counter can be read straight off the selection without a round-trip per
  // checkbox. Kept beside `selected` rather than replacing it because every
  // consumer only ever asks "is this url ticked". job_type isn't persisted
  // (only selectionOrder is), so a reload rebuilds it as null -- the 60/40
  // split counter goes blank for restored rows until re-ticked, but nothing
  // about queueing depends on it.
  const [selectedTypes, setSelectedTypes] = useState<Map<string, string | null>>(new Map())
  // The actual click sequence, tagged with which table each click came from
  // and that job's posted date. Selecting is a Set for O(1) membership
  // checks everywhere else, but the apply queue needs to preserve intent:
  // clicks are grouped into runs by table (a run ends when the user moves to
  // a different table), each run is sorted newest-posted-first internally,
  // and the runs stay in the order the user started them -- so a batch
  // picked in one table always lands ahead of a batch started afterward in
  // another, regardless of visual row order within either table.
  const [selectionOrder, setSelectionOrder] = useState<SelectionEntry[]>(initialOrder)
  const [launching, setLaunching] = useState(false)
  const [launchNote, setLaunchNote] = useState<string | null>(null)
  const [launchError, setLaunchError] = useState(false)
  const [dryRun, setDryRun] = useState(false)
  // Whether an apply run is already going -- if so, Launch must not attempt
  // to spawn a second one (that's the /api/launch 409). See handleLaunch.
  const { live } = useRunStream()

  useEffect(() => {
    try {
      localStorage.setItem(SELECTION_KEY, JSON.stringify(selectionOrder))
    } catch {
      // Private browsing / full storage: the selection just won't survive a
      // reload, same as before this existed.
    }
  }, [selectionOrder])

  function toggleSelect(url: string, jobType: string | null, tableId: string, posted: string | null) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
    setSelectedTypes((prev) => {
      const next = new Map(prev)
      if (next.has(url)) next.delete(url)
      else next.set(url, jobType)
      return next
    })
    setSelectionOrder((prev) => {
      if (prev.some((e) => e.url === url)) return prev.filter((e) => e.url !== url)
      return [...prev, { url, tableId, posted }]
    })
  }

  // Groups consecutive same-table entries into runs, sorts each run by
  // posted date descending (nulls last), and concatenates runs in the order
  // they started.
  function queueOrder(): string[] {
    const runs: { tableId: string; entries: typeof selectionOrder }[] = []
    for (const entry of selectionOrder) {
      const last = runs[runs.length - 1]
      if (last && last.tableId === entry.tableId) last.entries.push(entry)
      else runs.push({ tableId: entry.tableId, entries: [entry] })
    }
    return runs.flatMap((run) =>
      [...run.entries]
        .sort((a, b) => (b.posted ?? "").localeCompare(a.posted ?? ""))
        .map((e) => e.url),
    )
  }

  function clearSelection() {
    setSelected(new Set())
    setSelectedTypes(new Map())
    setSelectionOrder([])
  }

  async function handleLaunch() {
    // Two real HTTP calls, deliberately kept as two steps: /api/queue marks
    // the selection 'queued' and prices it (harmless, reversible), then
    // /api/launch actually spawns `applypilot apply --queued <batch>` as a
    // background subprocess -- the one call in this app that submits real
    // forms and spends real money when dry_run is off.
    //
    // Only one run at a time, so if `live` says one is already going, this
    // button must behave as "add to the queue" and stop there -- calling
    // /api/launch anyway is a guaranteed 409, and the batch it would have
    // launched is recoverable later from the Dashboard's pending-batches
    // panel instead.
    setLaunching(true)
    setLaunchNote(null)
    setLaunchError(false)
    try {
      const queued = await api.queue(queueOrder())
      if (queued.queued === 0) {
        setLaunchNote(
          `Nothing to launch -- all ${queued.skipped} selected job(s) were already applied, ` +
            `in flight, or otherwise unqueueable.`,
        )
        clearSelection()
        return
      }

      if (live) {
        setLaunchNote(
          `Queued ${queued.queued} job(s) as batch ${queued.batch} (est. ` +
            `$${queued.estimate.expected.toFixed(2)}${queued.estimate.n_samples === 0 ? ", default" : ""}). ` +
            `A run is already in progress -- launch this batch from the Dashboard's pending-batches ` +
            `panel once it finishes.`,
        )
        clearSelection()
        return
      }

      const launch = await api.launch(queued.batch, { workers: 1, dry_run: dryRun })
      setLaunchNote(
        `${dryRun ? "[DRY RUN] " : ""}Launched batch ${queued.batch}: ${launch.jobs} job(s), ` +
          `pid ${launch.pid} (est. $${queued.estimate.expected.toFixed(2)}` +
          `${queued.estimate.n_samples === 0 ? ", default" : ""}). Watch it on the Dashboard tab.`,
      )
      clearSelection()
      setTab("dashboard")
    } catch (e) {
      setLaunchError(true)
      setLaunchNote(
        `Failed to launch: ${String(e)}. Anything already queued is still queued -- ` +
          `retry from here, launch it from the Dashboard's pending-batches panel, or run ` +
          `\`applypilot apply --queued <batch>\` yourself.`,
      )
    } finally {
      setLaunching(false)
    }
  }

  return (
    <div className="app" id="app">
      <div className="app-bar">
        <span className="brand">applypilot</span>
        <div className="tabs">
          <button className={`tab${tab === "browse" ? " on" : ""}`} onClick={() => setTab("browse")}>
            Browse
          </button>
          <button
            className={`tab${tab === "dashboard" ? " on" : ""}`}
            onClick={() => setTab("dashboard")}
          >
            Dashboard
          </button>
          <button
            className={`tab${tab === "settings" ? " on" : ""}`}
            onClick={() => setTab("settings")}
          >
            Settings
          </button>
        </div>
        <LaunchBar
          selected={selected}
          onLaunch={handleLaunch}
          launching={launching}
          dryRun={dryRun}
          onDryRunChange={setDryRun}
          queueOnly={live}
        />
      </div>

      {launchNote && (
        <div
          style={{
            padding: "6px 16px",
            fontSize: 12,
            color: launchError ? "var(--a-bad)" : "var(--a-text-2)",
            background: "var(--a-sel-bg)",
            borderBottom: "1px solid var(--a-line)",
          }}
        >
          {launchNote}
        </div>
      )}

      {tab === "browse" && (
        <BrowseTab
          selected={selected}
          selectedTypes={selectedTypes}
          onToggleSelect={toggleSelect}
        />
      )}
      {tab === "dashboard" && <DashboardTab />}
      {tab === "settings" && <SettingsTab />}
    </div>
  )
}
