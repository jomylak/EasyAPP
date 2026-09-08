import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { EnvKeyName, EnvKeyStatus, LaneWeights, Settings } from "@/lib/types"
import { KNOWN_ENV_KEYS } from "@/lib/types"

const ENV_LABEL: Record<EnvKeyName, string> = {
  GEMINI_API_KEY: "Gemini (scoring)",
  OPENAI_API_KEY: "OpenAI",
  OPENROUTER_API_KEY: "OpenRouter (Goose)",
  LLM_URL: "Custom LLM URL",
  APPLYPILOT_JOB_PASSWORD: "Job site password",
}

// Null in a numeric field always means "use the built-in default" (per-job
// knobs) or "no cap" (daily ones) -- config.py's own convention, kept here so
// clearing a box and clearing the setting mean the same thing.
function numOrNull(v: string): number | null {
  const t = v.trim()
  if (t === "") return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

function NumField({
  value,
  onChange,
  placeholder,
  step,
}: {
  value: number | null
  onChange: (v: number | null) => void
  placeholder?: string
  step?: string
}) {
  return (
    <input
      className="srch setting-input"
      type="number"
      step={step}
      value={value ?? ""}
      placeholder={placeholder}
      onChange={(e) => onChange(numOrNull(e.target.value))}
    />
  )
}

/**
 * Backend/cost + limits + ranking weights + API keys.
 *
 * Deliberately does NOT duplicate the Dashboard's per-ATS pass/fail
 * breakdown or success-rate donut -- those are outcome diagnostics that
 * belong next to the live run panel and applications table they already
 * sit beside. This tab is for things that are actually configuration: what
 * the pipeline is allowed to do, and how much of a resume/key it's told.
 */
export function SettingsTab() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [envStatus, setEnvStatus] = useState<EnvKeyStatus | null>(null)
  const [envInputs, setEnvInputs] = useState<Partial<Record<EnvKeyName, string>>>({})
  const [ngW, setNgW] = useState<LaneWeights | null>(null)
  const [intW, setIntW] = useState<LaneWeights | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [savedNote, setSavedNote] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)

  useEffect(() => {
    api
      .settings()
      .then((s) => {
        setSettings(s)
        setNgW(s.new_grad_weights)
        setIntW(s.internship_weights)
      })
      .catch((e) => setError(String(e)))
    api.envKeys().then(setEnvStatus).catch(() => {})
  }, [])

  function flash(label: string) {
    setSavedNote(label)
    setTimeout(() => setSavedNote(null), 2500)
  }

  function set<K extends keyof Settings>(key: K, value: Settings[K]) {
    setSettings((prev) => (prev ? { ...prev, [key]: value } : prev))
  }

  async function saveSettings(patch: Partial<Settings>, label: string) {
    setSaving(label)
    try {
      const res = await api.updateSettings(patch)
      setSettings(res)
      flash(`${label} saved`)
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(null)
    }
  }

  async function saveApiKeys() {
    const toSend = Object.fromEntries(Object.entries(envInputs).filter(([, v]) => (v ?? "").trim()))
    if (Object.keys(toSend).length === 0) return
    setSaving("keys")
    try {
      await api.setEnvKeys(toSend)
      setEnvInputs({})
      const status = await api.envKeys()
      setEnvStatus(status)
      flash("API keys saved")
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(null)
    }
  }

  function weightRow(
    lane: "new_grad" | "internship",
    w: LaneWeights | null,
    setW: (next: LaneWeights) => void,
  ) {
    if (!w) return null
    return (["pay", "prestige", "location"] as const).map((k) => (
      <label className="setting" key={`${lane}-${k}`}>
        <span className="setting-label">{k}</span>
        <input
          className="srch"
          type="number"
          step="0.05"
          min="0"
          max="1"
          value={w[k]}
          onChange={(e) => setW({ ...w, [k]: Number(e.target.value) })}
        />
      </label>
    ))
  }

  if (error) return <div style={{ padding: 16, color: "var(--a-bad)" }}>Failed to load settings: {error}</div>
  if (!settings) return <div style={{ padding: 16, color: "var(--a-text-3)" }}>Loading settings…</div>

  return (
    <div className="dash">
      {savedNote && <div className="setting-saved">{savedNote}</div>}

      <section className="day-panel settings-section">
        <div className="dayhead">
          <span className="dayname">Backend &amp; cost</span>
        </div>
        <p className="settings-hint">
          Which engine drives the browser, what a run is estimated to cost before enough real history exists, and
          whether a stuck Goose run gets one retry on Claude.
        </p>

        <div className="setting-row">
          <label>Apply backend</label>
          <select
            className="select-field setting-input"
            value={settings.apply_backend}
            onChange={(e) => set("apply_backend", e.target.value as Settings["apply_backend"])}
          >
            <option value="goose">Goose (default, cheap)</option>
            <option value="claude">Claude Code</option>
          </select>
        </div>
        <div className="setting-row">
          <label>Fallback backend on stuck run</label>
          <select
            className="select-field setting-input"
            value={settings.apply_fallback_backend ?? ""}
            onChange={(e) => set("apply_fallback_backend", e.target.value || null)}
          >
            <option value="">Disabled</option>
            <option value="claude">Claude</option>
            <option value="goose">Goose</option>
          </select>
        </div>
        <div className="setting-row">
          <label>Default cost — Goose ($/app)</label>
          <input
            className="srch setting-input"
            type="number"
            step="0.01"
            value={settings.cost_defaults?.goose ?? ""}
            onChange={(e) => set("cost_defaults", { ...settings.cost_defaults, goose: Number(e.target.value) || 0 })}
          />
          <span className="setting-note">used only until real runs give a median</span>
        </div>
        <div className="setting-row">
          <label>Default cost — Claude ($/app)</label>
          <input
            className="srch setting-input"
            type="number"
            step="0.01"
            value={settings.cost_defaults?.claude ?? ""}
            onChange={(e) => set("cost_defaults", { ...settings.cost_defaults, claude: Number(e.target.value) || 0 })}
          />
        </div>
        <div className="setting-row">
          <label>Tailoring enabled</label>
          <input
            type="checkbox"
            checked={settings.tailoring_enabled}
            onChange={(e) => set("tailoring_enabled", e.target.checked)}
          />
          <span className="setting-note">off = every apply uses the base resume, untailored</span>
        </div>
        <div className="setting-row">
          <label>Cover letters enabled</label>
          <input
            type="checkbox"
            checked={settings.cover_letters_enabled}
            onChange={(e) => set("cover_letters_enabled", e.target.checked)}
          />
        </div>
        <div className="setting-row">
          <label>Goose may write ATS quirks cache</label>
          <input
            type="checkbox"
            checked={settings.goose_writes_quirks}
            onChange={(e) => set("goose_writes_quirks", e.target.checked)}
          />
        </div>
        <div className="setting-actions">
          <button
            className="btn"
            disabled={saving === "backend"}
            onClick={() =>
              saveSettings(
                {
                  apply_backend: settings.apply_backend,
                  apply_fallback_backend: settings.apply_fallback_backend,
                  cost_defaults: settings.cost_defaults,
                  tailoring_enabled: settings.tailoring_enabled,
                  cover_letters_enabled: settings.cover_letters_enabled,
                  goose_writes_quirks: settings.goose_writes_quirks,
                },
                "backend",
              )
            }
          >
            {saving === "backend" ? "Saving…" : "Save"}
          </button>
        </div>
      </section>

      <section className="day-panel settings-section">
        <div className="dayhead">
          <span className="dayname">Limits</span>
        </div>
        <p className="settings-hint">
          Leave a box blank to use the built-in default (per-job knobs) or to leave it uncapped (the two daily
          limits). Hitting a daily cap stops new jobs from being picked up for the rest of the day -- a job already
          running finishes rather than being killed mid-application.
        </p>

        <div className="setting-row">
          <label>Retries per job</label>
          <NumField
            value={settings.max_apply_attempts}
            onChange={(v) => set("max_apply_attempts", v)}
            placeholder="3 (default)"
          />
        </div>
        <div className="setting-row">
          <label>Max run time — Goose (s)</label>
          <NumField value={settings.goose_timeout} onChange={(v) => set("goose_timeout", v)} placeholder="2400 (default)" />
        </div>
        <div className="setting-row">
          <label>Max run time — Claude (s)</label>
          <NumField value={settings.apply_timeout} onChange={(v) => set("apply_timeout", v)} placeholder="300 (default)" />
        </div>
        <div className="setting-row">
          <label>Max turns — Goose</label>
          <NumField value={settings.goose_max_turns} onChange={(v) => set("goose_max_turns", v)} placeholder="300 (default)" />
        </div>
        <div className="setting-row">
          <label>Max repeated tool calls — Goose</label>
          <NumField
            value={settings.goose_max_tool_repetitions}
            onChange={(v) => set("goose_max_tool_repetitions", v)}
            placeholder="15 (default)"
          />
        </div>
        <div className="setting-row">
          <label>Max spend per day ($)</label>
          <NumField
            value={settings.max_daily_spend_usd}
            onChange={(v) => set("max_daily_spend_usd", v)}
            placeholder="no cap"
            step="0.5"
          />
        </div>
        <div className="setting-row">
          <label>Max applications per day</label>
          <NumField
            value={settings.max_daily_applications}
            onChange={(v) => set("max_daily_applications", v)}
            placeholder="no cap"
          />
        </div>
        <div className="setting-actions">
          <button
            className="btn"
            disabled={saving === "limits"}
            onClick={() =>
              saveSettings(
                {
                  max_apply_attempts: settings.max_apply_attempts,
                  goose_timeout: settings.goose_timeout,
                  apply_timeout: settings.apply_timeout,
                  goose_max_turns: settings.goose_max_turns,
                  goose_max_tool_repetitions: settings.goose_max_tool_repetitions,
                  max_daily_spend_usd: settings.max_daily_spend_usd,
                  max_daily_applications: settings.max_daily_applications,
                },
                "limits",
              )
            }
          >
            {saving === "limits" ? "Saving…" : "Save"}
          </button>
        </div>
      </section>

      <section className="day-panel settings-section">
        <div className="dayhead">
          <span className="dayname">Ranking</span>
        </div>
        <p className="settings-hint">
          How a desirability score is built, per lane. Pay, prestige and location are three independent
          components — pay used to be folded into location, which meant a New York posting scored identically
          at $83k and $354k. Weights are renormalised, so they don't have to sum to 1. Skills fit is not here
          on purpose: it's a tiebreaker only, and big-tech postings are pinned above the ranking entirely.
          Changing these costs nothing to apply — run <code>applypilot recompute</code>, no re-scoring needed.
        </p>
        <div className="settings-grid">
          <div className="settings-subhead">New grad</div>
          {weightRow("new_grad", ngW, setNgW)}
          <div className="settings-subhead">Internship</div>
          {weightRow("internship", intW, setIntW)}
        </div>
        <div className="setting-actions">
          <button
            className="btn"
            disabled={saving === "weights"}
            onClick={() =>
              saveSettings(
                { new_grad_weights: ngW ?? undefined, internship_weights: intW ?? undefined },
                "weights",
              )
            }
          >
            {saving === "weights" ? "Saving…" : "Save"}
          </button>
        </div>
      </section>

      <section className="day-panel settings-section">
        <div className="dayhead">
          <span className="dayname">API keys</span>
        </div>
        <p className="settings-hint">
          Written to ~/.applypilot/.env on this machine only. A key already set is never shown back here -- leave a
          box blank to keep it unchanged.
        </p>
        {KNOWN_ENV_KEYS.map((key) => (
          <div className="env-row" key={key}>
            <label>{ENV_LABEL[key]}</label>
            <input
              className="srch setting-input"
              type="password"
              autoComplete="off"
              placeholder={envStatus?.[key] ? "•••••••• (set)" : "not set"}
              value={envInputs[key] ?? ""}
              onChange={(e) => setEnvInputs((prev) => ({ ...prev, [key]: e.target.value }))}
              style={{ width: 260 }}
            />
            <span className={`badge ${envStatus?.[key] ? "applied" : "manual"}`}>
              {envStatus?.[key] ? "set" : "not set"}
            </span>
          </div>
        ))}
        <div className="setting-actions">
          <button className="btn" disabled={saving === "keys"} onClick={saveApiKeys}>
            {saving === "keys" ? "Saving…" : "Save keys"}
          </button>
        </div>
      </section>
    </div>
  )
}
