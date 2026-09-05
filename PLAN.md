# PLAN.md — ApplyPilot Web UI

> **Handoff document.** This is the working plan for the web frontend.
> Future sessions: read this file first, then check the **Progress** section at
> the bottom for what is done and what is next. Update Progress as you go.
>
> Created 2026-09-05.

# ApplyPilot Web UI — human-in-the-loop job triage

## Context

The pipeline is about to produce ~6000 jobs per two weeks once AI/ML and the
other tags are added. Applying to even the top slice of that is more than one
person can review, and every apply run costs real money ($0.045 on Goose,
~$1.16 on Claude per the 3 rows that actually have cost data).

The fix is to stop treating "apply" as an automatic consequence of "scored
highly" and put a human in the loop. This builds an InternList-style browsing
frontend where jobs are grouped by day, each day is its own filterable table,
and the user checks the jobs they want. Only checked jobs get applied to.

Today the only UI is `view.py`, which regenerates a **21 MB static HTML file**
with every full description inlined, no pagination, no apply state, and colors
keyed to site names that no longer exist. It gets replaced.

**Decisions already made:** React + Vite + Tailwind, TanStack Table + shadcn/ui,
dark spreadsheet look, headless Chrome for applies, background launch with live
Tab 2, per-job Remove (queued) and Stop (in-flight). Milestone 1 is Browse
end-to-end.

### Keep it local — yes

Bind `127.0.0.1` only, hardcoded. This serves resume PDFs, the personal profile,
and drives a Chrome profile holding the user's logged-in sessions. The deleted
`apply/fileserver.py` already learned this lesson verbatim: *"Loopback only.
Never bind 0.0.0.0 -- this serves personal documents."* Friends clone the repo,
`pip install -e .`, `applypilot serve`. No Node needed — `web/dist` is committed.

AGPL note (`README.md:222`): local use is unrestricted; hosting a modified copy
as a service would trigger the source-release clause. Another reason not to.

---

## What already exists (do not rebuild)

- **The fit/desirability split is already in the DB.** `fit_score`,
  `desirability_score` (REAL), and `company_prestige` (INTEGER 1–10) are
  separate columns (`database.py:193-204`). "Top 10 by pure prestige" is a sort
  on an existing column. No scoring rework needed — only exposure in the UI.
- **The apply queue is the `jobs` table.** `acquire_job()`
  (`launcher.py:64-197`) does a `BEGIN IMMEDIATE` → `SELECT ... LIMIT 1` →
  `UPDATE apply_status='in_progress', agent_id=?` atomic claim. That row-level
  claim is the entire concurrency model and it is already correct for
  user-selected batches — it just needs a different `WHERE`/`ORDER BY`.
- **`apply/dashboard.py` is the single progress choke point.** Every backend
  already calls `update_state()` (`dashboard.py:58`) and `add_event()`
  (`dashboard.py:78`), both under one lock. Publishing from inside those two
  functions surfaces live state without touching either backend.
- `router.explain_route()` (`scoring/router.py:151`) returns
  `{track, matched_on, candidates}` — feeds the expanded row's "why this
  resume" panel directly.
- `database.fit_gate_sql()` (`database.py:478`) and `get_stats()`
  (`database.py:285`) exist and are reusable, with caveats below.

## Constraints found during exploration

| Constraint | Where | Consequence |
|---|---|---|
| `launcher.main()` installs a **global SIGINT handler** and `atexit` hooks | `launcher.py:55,751` | The launcher must stay a **subprocess**, never in-process in the server |
| Live worker state is **process-local memory**, lost on exit | `dashboard.py:42-45` | Needs a shared write for the server to see it |
| **Zero indexes** on `jobs` beyond the URL PK | verified via `PRAGMA` | 5.1k rows today, 6k/2wk incoming — every filtered read is a full scan |
| `score_reasoning` is `keywords + "\n" + reasoning` | `scorer.py:869` | Read the `keywords` **column**; don't repeat `view.py:160`'s string-parsing |
| `apply_attempts == 99` is the permanent-failure sentinel | `launcher.py:314` | Never display it as a count |
| `apply_status='in_progress'` has **no heartbeat** | `launcher.py:187` | One stale row exists now; the UI must reconcile against a live pid |
| `company` is NULL until scored; `site` is the board, not the employer | memory + `database.py:188` | Browse table must handle unscored rows gracefully |
| No cost estimator exists anywhere in the codebase | grepped `src/` | Must be written from scratch |
| `tailoring_enabled: false` in live settings | `tailor.py:569` | `tailored_resume_path` is a copied base resume, not a tailored one |

---

## Architecture

```
Browser  ── React / Vite / Tailwind / shadcn / TanStack Table
   │  REST + SSE
FastAPI  ── `applypilot serve`, 127.0.0.1:8420
   │         owns queue mutations; never runs the launcher in-process
   ├── SQLite (~/.applypilot/applypilot.db)      durable job + outcome state
   ├── run_state.json (~/.applypilot/)           live worker state, ~2 Hz
   └── spawns ──> `applypilot apply --queued <batch> --headless`
                    (launcher.main, process model unchanged)
```

**Why a JSON file and not a DB table for live state:** per-tool-call updates at
1–2 Hz per worker would add WAL write contention against the same workers'
`mark_result` commits, for no durability benefit — `mark_result` already *is*
the durable record. Atomic write via temp file + `os.replace`.

---

## Backend changes

### 1. Schema — `src/applypilot/database.py`

Add to `_ALL_COLUMNS` (`database.py:152-246`); `ensure_columns()` migrates
automatically, forward-only, as every other column has been added:

- `queued_at TEXT` — when the user checked it
- `queue_batch TEXT` — uuid grouping one Launch press
- `queue_position INTEGER` — order within the batch

`apply_status` gains a `'queued'` value. Document it in the column comment
alongside the existing vocabulary.

**Indexes** — add to `init_db()`, all `CREATE INDEX IF NOT EXISTS`:
`apply_status`, `fit_score`, `desirability_score`, `company_prestige`, `site`,
`detail_scraped_at`, `queue_batch`, and an expression index on
`COALESCE(posted_date, discovered_at)` for the day bucketing.

### 2. Queued mode — `src/applypilot/apply/launcher.py`

Add `queue_batch: str | None = None` to `acquire_job()`. When set, replace the
ranking branch (`launcher.py:96-167`) with:

```sql
WHERE apply_status = 'queued' AND queue_batch = ?
ORDER BY queue_position, url
LIMIT 1
```

Skip `fit_gate`, `min_score`, pay floor, eligibility, and age decay entirely in
this mode — **the user's selection is the filter**, and re-gating it would
silently drop jobs they explicitly chose. Keep the manual-ATS short-circuit
(`launcher.py:173-183`) and the blocked-site checks; those are safety, not
ranking. The `BEGIN IMMEDIATE` claim stays byte-for-byte as it is.

Thread `--queued <batch>` through `launcher.main()` and `cli.py:155 apply`.

### 3. Live state publishing — `src/applypilot/apply/dashboard.py`

Add a module-private `_publish()` called at the end of `update_state()` and
`add_event()`, already inside `_lock`. Debounce to ~500 ms. Writes:

```json
{"pid": 1234, "batch": "…", "started_at": "…", "backend": "goose",
 "workers": [{…WorkerState as dict, plus "url"…}],
 "events": ["…"], "totals": {"applied": 0, "failed": 0, "cost": 0.0}}
```

`WorkerState` (`dashboard.py:22-38`) gains a `url` field so the UI can join a
running worker to its table row. Delete the file on clean launcher exit. The
Rich `Live` dashboard keeps working untouched.

### 4. Cost estimator — new `src/applypilot/costs.py`

Nothing like this exists. `estimate_batch(urls, backend) -> dict`:

- Median `apply_cost_usd` over history grouped by `(apply_backend, ats)`
- Fall back to median by `apply_backend` alone
- Fall back to `settings.json → cost_defaults`, seeded from the measured
  numbers in `HANDOFF.md:60-65`: `{goose: 0.05, claude: 1.20}`

Return `{low, expected, high, n_samples, basis}`. **`n_samples` matters**: only
3 rows in the DB have cost data today. The UI must render "~$0.90 (est. from 3
samples)" and not a fake-precise number.

### 5. Server — new `src/applypilot/web/server.py` + `applypilot serve`

FastAPI, `uvicorn`, `host="127.0.0.1"` hardcoded, default port 8420.
`--port` flag; no `--host` flag at all, so it cannot be widened by accident.

| Endpoint | Purpose |
|---|---|
| `GET /api/days` | day buckets + per-day counts (one `GROUP BY`) |
| `GET /api/jobs` | one day's page: `day`, `sort`, `page`, `page_size`, filters |
| `GET /api/jobs/{url}` | expansion payload: full description, keywords, reasoning, resume variant + `router.explain_route()` |
| `GET /api/facets` | distinct sites / ATSes / job types for filter dropdowns |
| `POST /api/queue` | `{urls[]}` → mark `'queued'`, return `batch` + cost estimate |
| `DELETE /api/queue` | `{urls[]}` → unqueue (the **Remove** action) |
| `POST /api/launch` | `{batch}` → spawn the apply subprocess, return pid |
| `POST /api/stop` | `{url}` → **Stop**: kill that worker, reset `apply_status` |
| `POST /api/stop-all` | kill the run |
| `GET /api/events` | SSE stream of `run_state.json` |
| `GET /api/stats` | Dashboard tiles |
| `GET /api/applications` | Tab 2 table |
| `GET /api/resume/{url}` | serve the tailored PDF (loopback only) |

**Do not call `get_stats()` per poll** — it runs ~20 full table scans
(`database.py:285-439`). Write a narrower stats query for `/api/stats`.

**Stale-lock reconciliation:** on startup and on every `/api/stats`, any row
with `apply_status='in_progress'` whose pid is absent from `run_state.json` gets
reset. One such stale row exists in the DB right now.

---

## Frontend

```
web/
  package.json  vite.config.ts  tailwind.config.ts  components.json
  src/
    App.tsx                    3-tab shell, owns global selection Set<url>
    tabs/BrowseTab.tsx         global filter bar + day sections
    tabs/DashboardTab.tsx      tiles + live run + applications table
    tabs/SettingsTab.tsx       usage, pass/fail, resume variants
    components/DayTable.tsx    ★ one instance per day, own TanStack state
    components/JobRow.tsx      inline downward expansion
    components/FilterBar.tsx   reused globally AND per-day
    components/LaunchBar.tsx   sticky top-right: count + est. cost + Launch
    components/ui/*            shadcn primitives (copied in, owned)
    lib/api.ts  lib/useRunStream.ts
  dist/                        committed, so friends need no Node
```

### `DayTable` — the core component

Each day section is a **fully independent table**, not a filtered view of one
global table. Props `{date, defaultPageSize}`; owns its own TanStack Table
instance with its own sorting, filtering, and pagination state. Sep 4 can be
sorted by prestige while Sep 3 is sorted by fit.

- **Quick chips** — `Top Prestige` (sort `company_prestige` desc, top 10),
  `Best Fit` (`desirability_score >= 6 AND fit_score >= 8`), `All`. These are
  presets over that day's own filter state; pressing one sets it, and the user
  can then hand-adjust from there.
- **Per-day filter bar** — the same full filter set as the global bar, scoped
  to this day only.
- **Default page sizes by recency**: today 30, last 3 days 20, older 10.
- **Resizable** — drag handle on the bottom edge adjusts that day's page size.

The **global filter bar** at the top of the tab is the "what am I willing to
look at at all" layer (source, job type, location, pay floor, ATS, eligibility,
keyword) and narrows every day.

Selection is a `Set<url>` in `App.tsx`, so it survives across days and tab
switches and drives the sticky `LaunchBar`.

**Row expansion** keeps everything on one page: clicking a row expands it
downward in place with full description, score reasoning, keywords, pay,
eligibility, ATS, and the resume variant with its routing explanation. Never
navigate away — that's the specific InternList/Jobright annoyance being fixed.

### Dashboard tab

Tiles (applied / in-flight / failed / queued / spend), then the live run panel
fed by SSE with per-worker status and last action, then the applications table
— the **same `DayTable` component**, different columns — showing status,
outcome, cost, and the tailored resume as a link to `/api/resume/{url}`.
Per-row `Remove` on queued, `Stop` on running, `Retry` on failed.

---

## Design step (do this first)

1. Drive Chrome to capture: Linear's issue list, Vercel dashboard (Geist),
   `ui.shadcn.com` DataTable demo, a Notion database view, and InternList as
   the baseline. Assemble into one side-by-side comparison page.
2. Once a direction is picked, use the `design` skill to lay out all three tabs
   as editable artboards on one canvas — dark, dense, spreadsheet-first.
3. Use the `dataviz` skill for the Dashboard tab's tiles and charts.
4. Build React to match the approved canvas.

---

## Milestones

0. **Design** — captures → comparison → canvas → approve.
1. **Backend seams** — columns, indexes, queued mode, `_publish()`, `costs.py`,
   `serve` + API. Fully testable with `curl` before any React exists.
2. **Browse tab** — the real deliverable; day tables, filters, expansion,
   selection, cost, Launch.
3. **Dashboard tab** — tiles, SSE live run, applications table, Remove/Stop.
4. **Settings tab** — usage, pass/fail rates, resume variant swapping.

---

## Files touched

| File | Change |
|---|---|
| `src/applypilot/database.py` | 3 columns in `_ALL_COLUMNS`; indexes in `init_db()` |
| `src/applypilot/apply/launcher.py` | `queue_batch` param on `acquire_job()`; thread through `main()` |
| `src/applypilot/apply/dashboard.py` | `_publish()`; `url` on `WorkerState` |
| `src/applypilot/cli.py` | `--queued` on `apply`; new `serve` command |
| `src/applypilot/costs.py` | **new** — batch cost estimator |
| `src/applypilot/web/server.py` | **new** — FastAPI app |
| `web/**` | **new** — React app + committed `dist/` |
| `src/applypilot/view.py` | delete once Browse ships |
| `pyproject.toml` | `fastapi`, `uvicorn[standard]`; hatch artifact for `web/dist` |
| `README.md`, `SETUP.md`, `CHANGELOG.md` | see below |

**Docs need an explicit correction.** `SETUP.md:200-206` and
`CHANGELOG.md:47-54` both go out of their way to say no server is needed, as a
deliberate decision when Skyvern was removed. This reverses that for a
different reason (a local UI, not a remote automation service) and the docs
should say so rather than quietly contradict themselves.

---

## Verification

- **Unit** (`tests/`, pytest + parametrize, matching existing style):
  `costs.py` estimation and fallback chain; `_flip_grad_year`-style pure tests
  for the queued-mode SQL builder; `run_state.json` round-trip.
- **API**: `applypilot serve`, then `curl` each endpoint. Confirm `/api/days`
  buckets correctly with NULL `posted_date`, and that queueing then unqueueing
  leaves `apply_status` back at NULL.
- **Index check**: `EXPLAIN QUERY PLAN` on the `/api/jobs` query — must show
  index use, not `SCAN jobs`.
- **UI**: drive the real app in Chrome via the browser tools; screenshot each
  tab; verify per-day independent sorting by sorting two days differently.
- **End-to-end**: select 2 jobs → Launch with `--dry-run` → confirm Tab 2
  updates live over SSE, `Stop` kills the worker and resets `apply_status`, and
  the dry run leaves no trace (`_restore_status`, `launcher.py:332`).
- **Fresh-clone check**: `pip install -e .` + `applypilot serve` in a clean
  venv with no Node installed, to confirm friends can actually run it.

---

## Risks worth flagging now

- **Enrichment throughput is the real bottleneck, not the UI.** 6000 jobs /
  2 weeks is ~430/day needing Playwright detail-scraping at `workers=1`. If
  enrichment can't keep up, the Browse table fills with rows that have no
  description, no ATS, and no apply URL. Out of scope here, but this plan's
  premise depends on it.
- **Scoring volume**: one Gemini call per job, 430/day, against free-tier rate
  limits. Same shape of risk.
- **Cost estimates start nearly blind** — n=3. The UI must be honest about that
  until history accumulates.

---

## Running it

```bash
applypilot serve                 # UI + API on http://127.0.0.1:8420
```

That serves the committed build in `src/applypilot/web/static/` and needs no
Node. Only use `cd web && npm run dev` when changing frontend code -- it serves
the UI on :5173 but proxies `/api` to :8420, so `applypilot serve` must be
running as well or every request 502s.

After changing anything in `web/src/`, run `cd web && npm run build`. It writes
straight into `src/applypilot/web/static/`, which is what `serve` mounts --
skipping the build means `serve` keeps showing the old UI.

---

## For a session picking this up

You are continuing an in-flight project. Before doing anything:

1. Read this entire file. The **Decisions log** at the bottom records choices
   that are settled -- do not reopen them, and do not "improve" on them without
   being asked.
2. Check the **Progress** table for what is done and what is next.
3. The design spec for the UI is `docs/design/day-table-mock.html` -- a
   working prototype of the Browse tab in plain HTML/JS, with the full token
   set in its `<style>` block. Open it in a browser and match it, rather than
   reinterpreting it from prose. (Its reference screenshots were stripped
   before committing; the prototype is the part that matters.)
4. Run `python3 -m pytest tests/ -q` and `ruff check src/` first, so you know
   whether anything was already broken before you touched it.

Ground rules learned the hard way on this project:

- **Never call `POST /api/launch`, and never run `applypilot apply` without
  `--dry-run`.** Both submit real job applications to real employers and spend
  real money. This is the one irreversible thing in the codebase.
- **Verify in a browser, not by reasoning.** Two of the three worst bugs here
  (a 34px dark band under every row, a Title column crushed to four
  characters) were invisible in the code and obvious in a screenshot.
- **Use real data.** The mock's short company names hid a table-layout bug that
  the real database exposed immediately. `~/.applypilot/applypilot.db` has
  5,000+ real rows; use them.
- **Do not work around a backend gap by changing the backend quietly.** If the
  API cannot do what the UI needs, say so and stop -- the Python is tested and
  a silent workaround hides a real gap. Two gaps found this way (`min_pay`,
  `pay_below_floor`) turned into proper server-side fixes.
- **Another session may be committing to this repo concurrently.** Check
  `git log` before and after long stretches of work, and never `git clean`.

## Progress

Update this table as work lands. `status` is one of: `todo`, `in progress`,
`done`, `blocked`.

| # | Milestone | Status | Notes |
|---|---|---|---|
| 0 | Design — Chrome captures, direction page, approval | approved | 2026-09-05. Direction signed off; feedback folded into the mock, which is now the build spec |
| 1 | Backend seams — schema, indexes, queued mode, `_publish()`, `costs.py`, `serve` + API | done | 2026-09-05. 183 tests pass; API verified against the live DB |
| 2 | Browse tab (React) | done | 2026-09-05. Verified in-browser against the live DB. 206 tests pass |
| 3 | Dashboard tab | done | 2026-09-05. Tiles, SSE live-run panel, applications table with Remove/Stop/Retry. Verified in-browser against the live DB |
| 4 | Settings tab | todo | |

### Milestone 1 checklist

- [x] `database.py` — `queued_at`, `queue_batch`, `queue_position` in `_ALL_COLUMNS`
- [x] `database.py` — indexes in `init_db()`
- [x] `launcher.py` — `queue_batch` param on `acquire_job()`
- [x] `launcher.py` — thread `queue_batch` through `main()`
- [x] `dashboard.py` — `url` field on `WorkerState`
- [x] `dashboard.py` — `_publish()` writing `~/.applypilot/run_state.json`
- [x] `costs.py` — `estimate_batch()`
- [x] `cli.py` — `--queued` on `apply`
- [x] `cli.py` — `serve` command
- [x] `web/server.py` — FastAPI app, all endpoints
- [x] `pyproject.toml` — `fastapi`, `uvicorn[standard]`
- [x] tests for `costs.py` + queued-mode SQL

### What Milestone 1 actually shipped

Beyond the plan, three bugs surfaced while building on this code and were
fixed because the UI would otherwise have inherited them:

- `acquire_job` returned `None` when it skipped a manual-ATS row, and
  `worker_loop` reads `None` as "queue empty". One unusable row partway down a
  batch would have stranded every job behind it as `queued` forever. Unusable
  rows now get a terminal status and the drain continues.
- `apply_status='in_progress'` had no heartbeat and nothing released it, so a
  killed launcher made its claimed rows permanently unreachable. `serve`
  sweeps them at startup and on every `/api/stats`.
- Both backends reported `job["site"]` as the company, so every dashboard row
  read "Intern List - SWE". They now report the `company` column.

API endpoints, all verified with curl against the live database:
`/api/days`, `/api/jobs`, `/api/job`, `/api/facets`, `/api/stats`,
`/api/applications`, `/api/resume`, `/api/queue`, `/api/queue/estimate`,
`/api/unqueue`, `/api/launch`, `/api/stop`, `/api/stop-all`, `/api/events`.

**Not yet exercised end to end:** `/api/launch` spawns a real apply run that
submits real forms, so it has only been verified as far as the subprocess
invocation. Drive it once with `dry_run: true` before trusting it.

### Browse tab build spec

The published mock is the spec. Build the React components to match it, not to
match this prose. Points the mock settles that are easy to get wrong:

- **Expansion content order**: full job description first (it is the thing
  being decided on), then the model's reasoning, then keywords, with type /
  ATS / resume / eligibility / pay-floor / cost in a right-hand column.
  Description panel scrolls at ~200px so one long posting cannot push the next
  twenty rows off screen, and running prose is capped at 78ch even though the
  table itself is full width.
- **Sanitise descriptions before rendering.** Some rows hold escaped markup
  rather than prose -- Adobe's arrives as `&lt;div&gt;&lt;p&gt;...`. Unescape
  and strip tags; never inject as HTML.
- **Filters are typed thresholds, not fixed buckets.** Every numeric column
  (prestige, fit, desirability, pay) takes any number the user types, per day.
  A day where nothing clears fit 8 must be answerable by typing 7.
- **Chips are presets over that same filter state**, not a parallel mechanism:
  pressing "Best Fit" fills in fit >= 8 and desirability >= 6, and the user
  adjusts from there.
- **Expansion animates both ways** via `grid-template-rows: 0fr -> 1fr` on a
  wrapper whose child is `overflow:hidden; min-height:0`. This animates to
  content height without hard-coding one. ~220ms, disabled under
  `prefers-reduced-motion`. The expansion row must stay mounted and toggle a
  class -- unmounting it snaps shut instead of animating.
- **Watch `tbody td { height: 34px }`.** It applies to the expansion cell too;
  without `height: 0` on the collapsed row, every job gets a dark 34px band
  under it. This bit once already.

### Pay normalisation (added 2026-09-05, Milestone 2)

The Browse tab's Pay threshold could not be served: `salary` is display text
("$35-$50/hr", "$110500-$160000/yr", "$6k-$11k/mon"), and both filtering and
sorting on it are wrong -- a lexical comparison puts "$9" above "$110500".

`src/applypilot/pay.py` normalises it to dollars per hour across all four
period formats and stores both ends in `pay_min_hourly` / `pay_max_hourly`.
`/api/jobs?min_pay=` compares against the **high** end, so "pay >= 40" asks
which jobs *could* pay at least that. `sort=pay` now orders numerically.

Two things worth not rediscovering:

- **Implausible figures are rejected, not clamped.** Discovery occasionally
  scrapes a number out of the wrong element -- the database holds
  `$7650000000-$12134000000/mon` -- and one such row is enough to wreck a sort
  or empty out a threshold. Anything over $1000/hr returns nothing. A
  wrong-but-plausible number is worse than no number, because nothing
  downstream can tell it was invented.
- **`-1` means "parsed and unusable", NULL means "not looked at yet".** Keep
  them distinct or the backfill re-parses the same unparseable strings on
  every startup. 46 rows are unusable (euros, CAD, "N/A"); non-dollar
  currencies are deliberately not converted rather than guessed at.

Independent corroboration that the parser is right: internships the scorer
marked above the $30 floor average $52.5/hr, and those it marked below average
$23.5/hr.

### Milestone 2 notes

Built by a Sonnet subagent against the milestone-0 mock. Files in `web/src/`:
`App.tsx` (tab shell, owns the selection Set), `tabs/BrowseTab.tsx`,
`components/DayTable.tsx` (one TanStack instance per day, independent state),
`JobExpansion.tsx`, `NumericFilter.tsx`, `GlobalFilterBar.tsx`,
`LaunchBar.tsx`, `lib/{api,types,utils}.ts`. Builds to
`src/applypilot/web/static/`, which is what `serve` mounts.

One layout bug the mock could not have caught: its company names were short,
so auto table layout gave Title its share. Real names run to "IBM (IBM
Consulting FutureNow Center / Technology & Red Hat practice)", and with
`white-space: nowrap` the Company column expanded to fit the longest name on
the page -- crushing Title to a four-character ellipsis and pushing Fit and
Des off the right edge. Fixed with `table-layout: fixed` plus an explicit
width per column and ellipsis on every truncatable cell. **Column widths must
not depend on which rows happen to be showing.**

Deliberate remaining work: the Launch button queues a batch and then tells the
user to run `applypilot apply --queued <batch>` by hand. Wiring it to
`POST /api/launch` is left undone because that endpoint submits real
applications and has never been run end to end.

### Post-Milestone-2 polish (2026-09-05)

Four requested fixes to the Browse tab, verified live in Chrome:

- **Windowed layout.** Day sections now render inside `.daylist` (side padding
  via `clamp(16px, 5vw, 64px)`) as bordered, rounded `.day-panel` cards instead
  of edge-to-edge sections, matching the InternList-style "table in the
  middle" feel the user asked for.
- **Per-day internal scroll.** Each day's table body lives in `.tablewrap`
  (sticky `thead`, its own scrollbar), capped at 14 visible rows before it
  scrolls internally — dragging the grip grows the box up to that cap, then
  further growth scrolls instead of lengthening the page. The side buffer is
  now where the user scrolls the page to move between days.
- **Grip drag fixed.** `onGripDown` in `DayTable.tsx` now calls
  `preventDefault()`, clears any existing selection, and sets
  `document.body.style.userSelect = "none"` for the drag's duration. Root
  cause: without `preventDefault()`, the drag doubled as a native text-select
  drag over the table -- functionally it still called `setPageSize`, but it
  rendered as a broken blue selection band, which read as "doesn't work."
  Confirmed fixed via dispatched mouse events (real automated screenshot
  coordinates don't map 1:1 to CSS px here because of a DPR-driven scale
  mismatch -- not a real bug, just a quirk of this browser-automation setup).
- **Numeric filter spinner spacing.** `.numf input` widened (34px → 40px)
  with `padding-right` and a small `margin-left` on the WebKit spin buttons,
  so `PRES ≥ 0` no longer has the up/down arrows flush against the digit.

### Infinite scroll + running index (2026-09-05)

Follow-up round, same session. Pagination (← page N/M →) removed entirely
from `DayTable.tsx` in favor of true infinite scroll within each day's own
box:

- Rows accumulate in one flat array per day (`rows`, not a single page).
  `CHUNK = 40` rows per fetch. `maybeLoadMore()` fires on the `tablewrap`'s
  own `onScroll` (near-bottom, 300px margin) *and* on an effect keyed to
  `[rows.length, total, boxHeight]` -- the second case matters because
  growing the box past its loaded content via the resize grip has no scroll
  event to hang a fetch off of; without it, dragging the box taller than 40
  rows' worth would just show blank space.
- A `requestIdRef` generation counter guards late responses: changing a
  filter/sort resets `rows` and bumps the id, and any in-flight fetch from
  before the change is dropped on arrival rather than appended to the new
  query's results.
- **Resize grip redefined.** It no longer maps drag distance to a row-count
  `pageSize` (that concept is gone -- data loading and visible box height are
  now decoupled). It now sets `boxHeight` in `DayTable.tsx` 1:1 with drag
  pixels (`MIN_BOX_HEIGHT=140`, `MAX_BOX_HEIGHT=900`), which is what the CSS
  `.tablewrap` renders as `max-height`. This is more direct than the old
  row-rounded version and was verified with dispatched `mousedown`/`mousemove`
  sequences at the box's actual `getBoundingClientRect()` -- 150px of drag
  produces exactly 150px of height change, no text-selection artifact.
- **Running index column** (`#`, `td.idx`) shows `row.index + 1` -- position
  in that day's own accumulated, sorted, filtered list. Since TanStack's
  `data` prop is the single flat `rows` array (not one page), this is already
  the correct running count with no extra bookkeeping.
- Caveat for whoever tests this in Chrome automation next: setting
  `el.scrollTop = x` via `javascript_tool` does **not** reliably dispatch a
  native `scroll` event in this environment, so `maybeLoadMore` won't fire
  from a JS-driven scroll -- use the `computer` tool's real `scroll` action
  (mouse wheel), which does. Also, `computer` tool screenshot pixel
  coordinates are not 1:1 with `getBoundingClientRect()` CSS pixels here
  (scale factor from a DPR/viewport mismatch, observed ~1.09x) -- for
  precise element targeting (like the resize grip), dispatch synthetic mouse
  events against the measured rect instead of eyeballing screenshot pixels.

Not yet done: no visual regression check of very tall boxes (900px cap) or
of the "nothing clears these filters" empty state's new 9-column colSpan.

### "Needs attention" cross-day panels (2026-09-05)

Two panels above the day list -- `AttentionPanel.tsx`, rendered twice from
`BrowseTab.tsx` inside `.attention-grid` (CSS grid, `auto-fit minmax(480px,
1fr)`, so it's side-by-side on wide screens and stacks on narrow ones without
a media query). Both are capped at top 50, floor `min_prestige >= 6`
(`ATTENTION_MIN_PRESTIGE` in `BrowseTab.tsx`), `unapplied_only: true` always
(independent of the global filter bar's own "Not yet applied" pill), narrowed
by the rest of the global filter bar (site/job type/ATS/eligible/pay floor/
search). Left panel sorts `prestige`, right sorts `fit`.

Two things worth not rediscovering:

- **No backend work was needed.** `/api/jobs`'s `day` param was already
  optional (`queries._filter_clauses` only adds the day clause `if
  f.get("day")`), so omitting it was already a valid cross-day query. The
  "new cross-day endpoint" flagged as a design risk when this was first
  proposed turned out not to exist as a gap.
- **No dedup logic for overlap between panels.** The same job legitimately
  appears in both (top by prestige and top by fit aren't disjoint sets) and
  can also appear in a day table further down. Checking it in one place
  shows it checked everywhere, for free, because `selected` is the one
  `Set<url>` `App.tsx` owns for the whole tab and every panel/table reads and
  writes the same instance. Verified by finding a job present in both
  panels' API responses, clicking its checkbox in Top Prestige, and
  confirming Best Fit's checkbox for the same url flipped to checked without
  any extra wiring.

`RowPair`, `columns`, `HEADERS`, `COL_COUNT`, `ROW_HEIGHT`, `HEADER_HEIGHT`
are now exported from `DayTable.tsx` so `AttentionPanel.tsx` can reuse the
same row rendering and column layout rather than forking it.

### Needs-attention panels: unbounded + sortable + windowed (2026-09-05)

Follow-up round: the top-50 cap and fixed sort were both cut per feedback.
`AttentionPanel.tsx` was rewritten to match `DayTable.tsx`'s own infinite
scroll almost exactly (same `CHUNK`/`requestIdRef`/`maybeLoadMore`/resize-grip
shape) rather than being a stripped-down variant -- it's now genuinely
unbounded, resizable, and each panel sorts independently by clicking any
column header (`sort` is now its own state seeded from a `defaultSort` prop,
not a fixed value).

- **New filter: `posted_within_days`.** Added to `queries._filter_clauses`
  (`src/applypilot/web/queries.py`) as `{_DAY_EXPR} >= date('now', ?)`, which
  reuses the exact expression `idx_jobs_day` is built on, so the range scan
  can use that index. Threaded through `/api/jobs` (optional int, default
  None = no window) and `web/src/lib/api.ts`'s `JobsQuery`. Tested in
  `tests/test_web_queries.py` with rows dated relative to `datetime.now()`,
  not the fixture's hardcoded 2026-09-0x dates -- a window filter is relative
  to the real clock, so pinning it to fixture dates would silently stop
  meaning "recent" once real time moves past them.
- **User-facing control**: a `NumericFilter` labeled "Posted within" in
  `BrowseTab.tsx`'s attention heading, default 7 days, shared by both panels.
  Clearing it (not zero -- `NumericFilter` already treats 0 as "unset")
  removes the window entirely, matching the ask to let the user pick it
  rather than hardcoding a week.
- **Caught by testing, not by reasoning**: the new backend filter appeared to
  do nothing at first -- `posted_within_days=1` returned the same total as no
  filter at all. Root cause was mundane: `applypilot serve` was already
  running from before these edits landed, and it's a plain `uvicorn` process
  with no `--reload`, so it was serving the pre-edit `queries.py`/`server.py`
  bytecode. Restarting the server process (not a code fix) resolved it --
  confirmed via direct `curl` against `/api/jobs` with `posted_within_days`
  at 1/3/7/30/365, which produced a monotonically increasing total that
  plateaus at the true count once the window exceeds the data's actual span.
  Worth remembering for any future backend edit tested against a
  long-running dev `serve` process.

### Milestone 3 notes (Dashboard tab, 2026-09-05)

No backend work needed -- `/api/stats`, `/api/applications`, `/api/stop`,
`/api/stop-all`, `/api/events` (SSE) were all already built and tested in
Milestone 1 but never wired to any UI. This milestone is entirely
`web/src/`:

- `lib/useRunStream.ts` -- a small hook subscribing to `/api/events`
  (`EventSource`); the server already only pushes on change, so no polling
  needed for the live panel itself.
- `components/ApplicationsTable.tsx` -- everything with an apply outcome.
  `/api/applications` has no pagination (a single indexed query capped at
  `limit`), so this fetches once and polls every 3s only while a run is
  actually live, unlike the day tables' infinite scroll. Per-row action is
  status-dependent: Remove (queued → `unqueue`), Stop (in_progress →
  `stop`), Retry (failed → re-`queue` the same url, since that's already
  exactly what re-queuing a single job does -- no new endpoint needed).
- `tabs/DashboardTab.tsx` -- stat tiles (applied/in-flight/failed/queued/
  spend/scored), the live-run panel (worker rows with status badge, current
  job, last action, elapsed time, and a Stop-all button gated on `live`),
  and the applications table.
- Loaded the `dataviz` skill before building the tiles per the system
  instruction to do so before any stat-tile/KPI-row work. Given this is a
  KPI row, not a chart, the relevant guidance was "a stat tile is a valid
  answer, not every number needs a chart" plus the status-color rule (good/
  warning/serious/critical reserved, never reused for identity) -- reused
  the app's own existing dark tokens (`--a-good`, `--a-bad`) and added one
  (`--a-warn`, borrowed from the already-shipped prestige ramp's warm end)
  rather than introducing a new palette needing the CVD validator, since no
  new categorical series was added.
- Status badges (`.badge`) are one shared component style used by both the
  live worker rows and the applications table, so a status reads the same
  color everywhere it appears -- deliberate, per the color-formula rule that
  identity/state should never be re-encoded per surface.

Not exercised: actually clicking Remove/Stop/Retry against the live database
during this session -- verified by reading the code path and by the tiles/
table rendering real data correctly, but not by mutating real apply-status
history as a side effect of testing. `/api/launch` remains untouched, per
the standing ground rule.

### Round of polish across both tabs (2026-09-05)

- **"Needs attention" renamed to "Priority"**, and the redundant subtitle
  next to the heading was dropped -- each panel already states its own
  `unapplied · prestige N+ · total` line, so the shared heading only needs
  the "posted within" widget now.
- **Pay column widened in the attention panels.** Same percentage widths as
  the day tables read fine at full width but left Pay too narrow at roughly
  half that width. Scoped override: `.attention-grid thead th.col-pay` to
  20%, `.col-location` down to 11%, rather than touching the day-table
  widths that were already right.
- **Row expansion now shows Company/Title/Pay/Location** above Type/ATS in
  `JobExpansion.tsx`'s right column -- previously only visible in the
  collapsed row itself, which is not visible once a row is expanded and
  scrolled past.
- **Applications table is now a real filtering/sorting surface**: status
  pills (All/Applied/Failed/Queued/In progress/Manual) driving
  `/api/applications?status=`, a company/title search box, an `Applied`
  date column separate from `Last attempt`, and clickable sortable headers
  -- all client-side over the one fetched page, since `/api/applications`
  has no server-side sort/pagination to hook into.
- **Cancel vs. Remove, fixed.** `/api/unqueue` gained an optional
  `revert_status` (whitelisted to `"failed"`). The Applications table's
  queued-row button is now always labeled "Cancel" and, when the row's
  `apply_attempts > 0` (i.e. it got to 'queued' via Retry, not a fresh
  queue), reverts it to `'failed'` instead of `NULL` -- previously it
  always cleared to NULL, which silently erased the fact that the job had
  ever failed and looked like the row had been deleted from the table.
- **New stats**: an overall success-rate donut (`components/Donut.tsx`) and
  avg-cost-per-attempt, plus a per-ATS breakdown (`components/
  AtsBreakdown.tsx`) reusing the already-built `/api/ats-stats` (built in
  Milestone 1, never wired to any UI until now). Deliberately **not** a
  donut per ATS: comparing several proportions by angle across separate
  small charts is the "pie chart forest" anti-pattern from the `dataviz`
  skill's own anti-patterns reference -- a single donut is legitimate for
  one value against its complement, so that's the only place one is used;
  the per-ATS comparison is a sortable table with an inline bar per row
  instead, which is the legible version of the same comparison.

### Two bugs found in review, fixed (2026-09-05)

- **Priority panels stretched together on drag.** `.attention-grid`'s default
  `align-items: stretch` made the un-dragged sibling `.day-panel` match the
  dragged one's CSS-grid row height, but that sibling's own `.tablewrap`
  kept its own unchanged `max-height` -- so it grew a grey empty strip
  instead of actually filling with more rows. Each panel already owns
  independent `boxHeight` state on purpose ([[applypilot-web-ui-direction]]);
  the fix is `align-items: start` on `.attention-grid` so the grid stops
  forcing sibling heights to match at all. Verified in Chrome: dragging Top
  Prestige down grew it to 17 real rows while Best Fit stayed at its own
  natural height with no grey band.
- **Retry inflated "Avg cost / attempt" before anything ran again.** That
  tile divided `spend` (SUM of `apply_cost_usd` over *every* row regardless
  of current status) by `applied + failed`. Retry flips a failed row's
  `apply_status` back to `'queued'` without clearing its historical cost --
  so clicking Retry shrank the denominator while the numerator stayed put,
  inflating the average for a click that hadn't spent anything yet.
  `queries.stats()` now also returns `priced_attempts` (`SUM(apply_cost_usd
  IS NOT NULL)`), which doesn't move on a status flip, and the tile divides
  by that instead. No new cost data source needed -- `apply_cost_usd` was
  already the real, launcher-recorded number; the bug was the denominator,
  not missing cost visibility.

### Decisions log

Record any decision a future session should not re-litigate.

- **2026-09-05** — Stack fixed: React + Vite + Tailwind, TanStack Table +
  shadcn/ui. Not Streamlit, not AG Grid, not hand-rolled tables.
- **2026-09-05** — Local only, bound to `127.0.0.1`, no `--host` flag. Friends
  run their own copy from a clone.
- **2026-09-05** — The launcher stays a **subprocess**. It installs a global
  SIGINT handler and `atexit` hooks (`launcher.py:55,751`) and must never be
  imported into the server process.
- **2026-09-05** — Live run state goes to a JSON file, not a DB table, to avoid
  WAL write contention with the apply workers' own commits.
- **2026-09-05** — No scoring rework. `fit_score`, `desirability_score`, and
  `company_prestige` already exist as separate columns; the UI just exposes
  them.
- **2026-09-05** — Each day section is an **independent table** with its own
  sort/filter/pagination state, not a filtered view of one global table.
- **2026-09-05** — Applying Chrome runs **headless**. Per-day tables paginate
  with recency-based defaults (30 rows today, 20 for the last three days, 10
  older) and a drag handle to resize.
- **2026-09-05** — Per-job **Stop** on an in-flight job works by killing that
  worker's Chrome; the backends' existing CDP watchdog then aborts the run and
  records it failed. This reuses a mechanism that already exists instead of
  building an IPC channel into the child process, at the cost of ~20s latency.
- **2026-09-05** — `/api/queue` and `/api/launch` are **separate steps**, so
  the cost estimate can be shown and reconsidered before anything is spent.
- **2026-09-05** — Cost estimates report `n_samples`. With three historical
  runs in the database, the UI must say so rather than print a confident
  number.
- **2026-09-05** — Per-application cost default is **$0.04** (Goose). Left
  alone deliberately; `costs.estimate_batch` switches to the observed median
  on its own once enough real runs accumulate, so this number should not need
  hand-tuning again.
- **2026-09-05** — The **React build runs on Sonnet**, against the published
  mock as spec. The backend seams and their tests are already done, so the
  frontend work is well-specified enough not to need the larger model.
