import { useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"

interface Props {
  url: string
  onClose: () => void
}

// Non-printable keys the page needs raw (Enter to submit, Tab to move
// fields, Backspace to edit) -- everything else goes through as a 'char'
// insertText event instead, since that's what actually produces the right
// character for shifted/composed keys without reimplementing a keymap.
const RAW_KEYS = new Set([
  "Enter", "Tab", "Backspace", "Delete", "Escape",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
])

/**
 * Modal remote-control window for the interactive login session (see
 * web/login_session.py): a live screencast of the persistent enrichment
 * Chrome profile, with clicks/typing forwarded back over the same
 * websocket via CDP Input events. One session at a time, closed on unmount.
 */
export function LoginWindow({ url, onClose }: Props) {
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting")
  const imgRef = useRef<HTMLImageElement>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    let cancelled = false
    api.openLoginSession(url).catch(() => !cancelled && setStatus("error"))

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/login-session`)
    ws.onopen = () => !cancelled && setStatus("live")
    ws.onerror = () => !cancelled && setStatus("error")
    ws.onclose = () => !cancelled && setStatus("error")
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
  }, [url])

  function send(msg: Record<string, unknown>) {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify(msg))
  }

  // Maps a click/mouse position on the (possibly CSS-scaled) <img> back to
  // real page coordinates -- the backend launches the browser at exactly
  // the screencast frame's own pixel size, so naturalWidth/Height IS the
  // page's viewport size and this is the only scaling needed.
  function toPageCoords(e: React.MouseEvent<HTMLImageElement>): { x: number; y: number } | null {
    const img = imgRef.current
    if (!img || !img.naturalWidth) return null
    const rect = img.getBoundingClientRect()
    const scaleX = img.naturalWidth / rect.width
    const scaleY = img.naturalHeight / rect.height
    return { x: (e.clientX - rect.left) * scaleX, y: (e.clientY - rect.top) * scaleY }
  }

  function handleMouse(e: React.MouseEvent<HTMLImageElement>, type: string) {
    e.preventDefault()
    const pos = toPageCoords(e)
    if (pos) send({ type, ...pos, button: "left" })
  }

  function handleWheel(e: React.WheelEvent<HTMLImageElement>) {
    const pos = toPageCoords(e)
    if (pos) send({ type: "wheel", ...pos, deltaX: e.deltaX, deltaY: e.deltaY })
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    e.preventDefault()
    if (RAW_KEYS.has(e.key)) {
      send({ type: "rawKeyDown", key: e.key, code: e.code })
      send({ type: "keyUp", key: e.key, code: e.code })
    } else if (e.key.length === 1) {
      send({ type: "char", text: e.key })
    }
  }

  const [reseedResult, setReseedResult] = useState<string | null>(null)

  function handleClose(reseedWorkers: boolean) {
    api
      .closeLoginSession(reseedWorkers)
      .then((res) => {
        if (reseedWorkers) {
          setReseedResult(
            res.reseeded.length
              ? `Synced to worker${res.reseeded.length === 1 ? "" : "s"} ${res.reseeded.join(", ")}.`
              : "Nothing to sync -- no worker profiles are initialized yet.",
          )
          // Give the user a moment to read the result before the window disappears.
          setTimeout(onClose, 2000)
        } else {
          onClose()
        }
      })
      .catch((e) => {
        setReseedResult(`Sync failed: ${e}`)
      })
  }

  return (
    <div className="loginwin-overlay" onClick={() => handleClose(false)}>
      <div className="loginwin" onClick={(e) => e.stopPropagation()}>
        <div className="loginwin-header">
          <span>Manual login &mdash; close when done</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="srch" onClick={() => handleClose(false)}>Close</button>
            <button className="btn" onClick={() => handleClose(true)}>
              Close &amp; sync to workers
            </button>
          </div>
        </div>
        {reseedResult && <div className="loginwin-reseed-note">{reseedResult}</div>}
        <div className="loginwin-body">
          {status !== "live" && (
            <div className="loginwin-status">
              {status === "connecting" ? "Connecting…" : "Connection lost — close and retry"}
            </div>
          )}
          <img
            ref={imgRef}
            className="loginwin-frame"
            hidden={status !== "live"}
            alt=""
            tabIndex={0}
            onMouseDown={(e) => handleMouse(e, "mousePressed")}
            onMouseUp={(e) => handleMouse(e, "mouseReleased")}
            onMouseMove={(e) => handleMouse(e, "mouseMoved")}
            onWheel={handleWheel}
            onKeyDown={handleKeyDown}
            onContextMenu={(e) => e.preventDefault()}
          />
        </div>
      </div>
    </div>
  )
}
