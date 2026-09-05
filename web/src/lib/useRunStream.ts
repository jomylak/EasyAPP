import { useEffect, useState } from "react"
import type { RunEvent } from "./types"

/**
 * Subscribes to /api/events (server-sent events over the run_state.json
 * mirror). The server only pushes when the payload actually changes, so an
 * idle page holds one quiet connection rather than polling.
 */
export function useRunStream(): RunEvent {
  const [event, setEvent] = useState<RunEvent>({ run: null, live: false })

  useEffect(() => {
    const source = new EventSource("/api/events")
    source.onmessage = (msg) => {
      try {
        setEvent(JSON.parse(msg.data) as RunEvent)
      } catch {
        // A malformed frame is not worth taking the connection down over.
      }
    }
    return () => source.close()
  }, [])

  return event
}
