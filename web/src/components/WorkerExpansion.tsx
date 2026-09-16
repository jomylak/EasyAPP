import { useEffect, useRef, useState } from "react"
import type { WorkerState } from "@/lib/types"

interface Props {
  worker: WorkerState
  open: boolean
}

// A worker is worth watching live only while it actually has a job open in
// Chrome. Once it's done/idle/failed the browser for that job is gone (or
// about to be), so there is nothing to stream -- connecting anyway would
// just spend a few seconds finding that out the hard way.
const LIVE_STATUSES = new Set(["starting", "applying", "captcha"])

type ConnState = "connecting" | "live" | "unavailable" | "closed"

/**
 * The expansion panel for a live worker row in the Runs tab: the worker's
 * own recent tool-call log on the left, a live CDP screencast of its Chrome
 * on the right. Mirrors JobExpansion's mount pattern -- stays mounted, the
 * parent animates the wrapper shut, and the screencast socket only opens
 * while `open` is true so a collapsed panel costs the VM nothing.
 */
export function WorkerExpansion({ worker, open }: Props) {
  const [connState, setConnState] = useState<ConnState>("connecting")
  const imgRef = useRef<HTMLImageElement>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!open || !LIVE_STATUSES.has(worker.status)) {
      setConnState("closed")
      return
    }

    setConnState("connecting")
    let cancelled = false
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/screencast/${worker.worker_id}`)
    ws.onopen = () => !cancelled && setConnState("live")
    ws.onclose = () => !cancelled && setConnState((s) => (s === "live" ? "closed" : "unavailable"))
    ws.onerror = () => !cancelled && setConnState("unavailable")
    ws.onmessage = (ev) => {
      if (!cancelled && imgRef.current) imgRef.current.src = `data:image/jpeg;base64,${ev.data}`
    }
    wsRef.current = ws

    return () => {
      cancelled = true
      ws.close()
      wsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, worker.worker_id, worker.status])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [worker.recent_actions])

  const tokensKnown = worker.input_tokens > 0 || worker.output_tokens > 0

  return (
    <div className="workerexp">
      <div className="workerexp-log" ref={logRef}>
        <h4>Recent actions</h4>
        {(worker.status === "captcha" || worker.status === "login_issue") && (
          <div className="workerexp-stats">
            {worker.status === "captcha" && <span className="badge failed">captcha hit</span>}
            {worker.status === "login_issue" && <span className="badge failed">login required</span>}
          </div>
        )}
        <div className="workerexp-stats">
          <span>{worker.actions} tool call{worker.actions === 1 ? "" : "s"}</span>
          <span>${worker.total_cost.toFixed(3)} this session</span>
          {tokensKnown && (
            <span title="Cumulative for this worker's session; updates once per finished job, not mid-job">
              {worker.input_tokens.toLocaleString()} in / {worker.output_tokens.toLocaleString()} out
              {worker.cache_read_tokens ? ` (${worker.cache_read_tokens.toLocaleString()} cached)` : ""}
            </span>
          )}
        </div>
        {worker.recent_actions.length === 0 ? (
          <p style={{ color: "var(--a-text-3)" }}>No actions logged yet.</p>
        ) : (
          <ul className="actionlog">
            {worker.recent_actions.map((line, i) => {
              const m = line.match(/^(\d\d:\d\d:\d\d) (.*)$/)
              const ts = m ? m[1] : null
              const rest = m ? m[2] : line
              const isReasoning = rest.startsWith("\u{1F4AD}")
              return (
                <li key={i} className={isReasoning ? "actionlog-reasoning" : undefined}>
                  {ts && <span className="actionlog-ts">{ts}</span>} {isReasoning ? rest.slice(2).trim() : rest}
                </li>
              )
            })}
          </ul>
        )}
      </div>

      <div className="workerexp-live">
        <h4>Live view</h4>
        <div className="livebox">
          {connState !== "live" && (
            <div className="livebox-status">
              {connState === "connecting" && "Connecting…"}
              {connState === "unavailable" && "Live view unavailable"}
              {connState === "closed" && "Not running"}
            </div>
          )}
          {/* Stays in the DOM (just hidden) across reconnects so the last
              frame doesn't flash blank between one screencast session and
              the next. */}
          <img ref={imgRef} className="livebox-frame" hidden={connState !== "live"} alt="" />
        </div>
      </div>
    </div>
  )
}
