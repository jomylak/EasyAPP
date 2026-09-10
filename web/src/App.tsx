import { useState } from "react"
import { LaunchBar } from "@/components/LaunchBar"
import { api } from "@/lib/api"
import { BrowseTab } from "@/tabs/BrowseTab"
import { DashboardTab } from "@/tabs/DashboardTab"
import { SettingsTab } from "@/tabs/SettingsTab"

type Tab = "browse" | "dashboard" | "settings"

export default function App() {
  const [tab, setTab] = useState<Tab>("browse")
  // A Set of urls held above the day tables, so it survives across days and
  // tab switches and drives the sticky launch bar.
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // url -> job_type for everything currently ticked, so the 60/40 split
  // counter can be read straight off the selection without a round-trip per
  // checkbox. Kept beside `selected` rather than replacing it because every
  // consumer only ever asks "is this url ticked".
  const [selectedTypes, setSelectedTypes] = useState<Map<string, string | null>>(new Map())
  // The actual click sequence, tagged with which table each click came from
  // and that job's posted date. Selecting is a Set for O(1) membership
  // checks everywhere else, but the apply queue needs to preserve intent:
  // clicks are grouped into runs by table (a run ends when the user moves to
  // a different table), each run is sorted newest-posted-first internally,
  // and the runs stay in the order the user started them -- so a batch
  // picked in one table always lands ahead of a batch started afterward in
  // another, regardless of visual row order within either table.
  const [selectionOrder, setSelectionOrder] = useState<
    { url: string; tableId: string; posted: string | null }[]
  >([])
  const [launching, setLaunching] = useState(false)
  const [launchNote, setLaunchNote] = useState<string | null>(null)
  const [launchError, setLaunchError] = useState(false)
  const [dryRun, setDryRun] = useState(false)

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

  async function handleLaunch() {
    // Two real HTTP calls, deliberately kept as two steps: /api/queue marks
    // the selection 'queued' and prices it (harmless, reversible), then
    // /api/launch actually spawns `applypilot apply --queued <batch>` as a
    // background subprocess -- the one call in this app that submits real
    // forms and spends real money when dry_run is off.
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
        setSelected(new Set())
        setSelectedTypes(new Map())
        setSelectionOrder([])
        return
      }

      const launch = await api.launch(queued.batch, { workers: 1, dry_run: dryRun })
      setLaunchNote(
        `${dryRun ? "[DRY RUN] " : ""}Launched batch ${queued.batch}: ${launch.jobs} job(s), ` +
          `pid ${launch.pid} (est. $${queued.estimate.expected.toFixed(2)}` +
          `${queued.estimate.n_samples === 0 ? ", default" : ""}). Watch it on the Dashboard tab.`,
      )
      setSelected(new Set())
      setSelectedTypes(new Map())
      setSelectionOrder([])
      setTab("dashboard")
    } catch (e) {
      setLaunchError(true)
      setLaunchNote(
        `Failed to launch: ${String(e)}. Anything already queued is still queued -- ` +
          `retry from here, or run \`applypilot apply --queued <batch>\` yourself.`,
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
