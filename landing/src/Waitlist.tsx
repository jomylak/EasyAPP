import { useEffect, useState } from "react"

type State = "idle" | "sending" | "done" | "error"

export default function Waitlist() {
  const [count, setCount] = useState<number | null>(null)
  const [state, setState] = useState<State>("idle")

  useEffect(() => {
    fetch("/api/count")
      .then((r) => r.json())
      .then((d: { count: number }) => setCount(d.count))
      .catch(() => {}) // count is decoration; the form works without it
  }, [])

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    setState("sending")
    try {
      const r = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), website: form.get("website") }),
      })
      if (!r.ok) throw new Error(String(r.status))
      setState("done")
      setCount((c) => (c === null ? c : c + 1))
    } catch {
      setState("error")
    }
  }

  if (state === "done") return <p className="text-[var(--a-good)]">You're on the list. Thanks!</p>

  return (
    <form onSubmit={submit} className="flex flex-col flex-wrap gap-3 sm:flex-row">
      {/* honeypot: real users never see or fill this */}
      <input name="website" tabIndex={-1} autoComplete="off" className="hidden" />
      <input
        name="email"
        type="email"
        required
        maxLength={254}
        placeholder="you@email.com"
        className="min-w-0 flex-1 rounded-md border border-[var(--a-line)] bg-[var(--a-surface)] px-3 py-2 outline-none focus:border-[var(--a-sel)]"
      />
      <button
        disabled={state === "sending"}
        className="rounded-md bg-[var(--a-sel)] px-4 py-2 font-medium text-black disabled:opacity-60"
      >
        Join the waitlist
      </button>
      {state === "error" && <p className="text-sm text-red-400 sm:basis-full">Something went wrong, try again.</p>}
      {count !== null && count > 0 && (
        <p className="text-sm text-[var(--a-text-2)] sm:basis-full">{count.toLocaleString()} people already on the list</p>
      )}
    </form>
  )
}
