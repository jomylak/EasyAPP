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
  const [launching, setLaunching] = useState(false)
  const [launchNote, setLaunchNote] = useState<string | null>(null)
  const [launchError, setLaunchError] = useState(false)
  const [dryRun, setDryRun] = useState(false)

  function toggleSelect(url: string, jobType: string | null) {
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
  }

  function toggleMany(rows: { url: string; job_type: string | null }[], checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev)
      for (const r of rows) {
        if (checked) next.add(r.url)
        else next.delete(r.url)
      }
      return next
    })
    setSelectedTypes((prev) => {
      const next = new Map(prev)
      for (const r of rows) {
        if (checked) next.set(r.url, r.job_type)
        else next.delete(r.url)
      }
      return next
    })
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
      const queued = await api.queue(Array.from(selected))
      if (queued.queued === 0) {
        setLaunchNote(
          `Nothing to launch -- all ${queued.skipped} selected job(s) were already applied, ` +
            `in flight, or otherwise unqueueable.`,
        )
        setSelected(new Set())
        setSelectedTypes(new Map())
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
          onToggleMany={toggleMany}
        />
      )}
      {tab === "dashboard" && <DashboardTab />}
      {tab === "settings" && <SettingsTab />}
    </div>
  )
}
