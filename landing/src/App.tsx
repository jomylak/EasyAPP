import Waitlist from "./Waitlist"

// ponytail: placeholder hero. The fake-dashboard demo (day table, Launch
// button, clip grid, metrics) replaces the demo slot once clips exist.
export default function App() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-8 px-4 py-16">
      <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">
        Pick the jobs. An AI agent fills out the applications.
      </h1>
      <p className="text-lg text-[var(--a-text-2)]">
        Works across Workday, Greenhouse, Lever, Ashby and more. You choose where to apply; it does the forms.
      </p>
      <div id="demo" />
      <Waitlist />
    </main>
  )
}
