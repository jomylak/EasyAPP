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
  const [launching, setLaunching] = useState(false)
  const [launchNote, setLaunchNote] = useState<string | null>(null)

  function toggleSelect(url: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
  }

  function toggleMany(urls: string[], checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev)
      for (const u of urls) {
        if (checked) next.add(u)
        else next.delete(u)
      }
      return next
    })
  }

  async function handleLaunch() {
    // /api/launch spends real money and submits real applications -- it is
    // wired here only as far as /api/queue (which just marks rows 'queued'
    // and prices them). Nothing calls /api/launch from this UI.
    setLaunching(true)
    setLaunchNote(null)
    try {
      const res = await api.queue(Array.from(selected))
      setLaunchNote(
        `Queued ${res.queued} job(s) as batch ${res.batch} ` +
          `(est. $${res.estimate.expected.toFixed(2)}` +
          `${res.estimate.n_samples === 0 ? ", default" : ""}). ` +
          `Run \`applypilot apply --queued ${res.batch}\` to actually apply -- ` +
          `this UI does not call /api/launch.`,
      )
      setSelected(new Set())
    } catch (e) {
      setLaunchNote(`Failed to queue: ${String(e)}`)
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
        <LaunchBar selected={selected} onLaunch={handleLaunch} launching={launching} />
      </div>

      {launchNote && (
        <div
          style={{
            padding: "6px 16px",
            fontSize: 12,
            color: "var(--a-text-2)",
            background: "var(--a-sel-bg)",
            borderBottom: "1px solid var(--a-line)",
          }}
        >
          {launchNote}
        </div>
      )}

      {tab === "browse" && (
        <BrowseTab selected={selected} onToggleSelect={toggleSelect} onToggleMany={toggleMany} />
      )}
      {tab === "dashboard" && <DashboardTab />}
      {tab === "settings" && <SettingsTab />}
    </div>
  )
}
