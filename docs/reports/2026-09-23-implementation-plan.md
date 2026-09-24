# Implementation plan: from the 2026-09-23 trace analysis

Companion to `2026-09-23-apply-trace-analysis.md`.

## Status: deployed 2026-09-23 07:48 UTC, harness `9ed306b97d4c`

| Item | State | Where / check |
|---|---|---|
| 1 Claim race | done (fix by session b0, regression test here) | `dedup.link`; `tests/test_apply_claim_race.py` fails 5/5 on the old behavior |
| 2 `applied` terminal | done | `launcher.mark_result`; test in the same file |
| 3 Reaper spares live agents | done | `orphan_reaper.driven_workers`; `tests/test_orphan_reaper.py` |
| 4 Infra circuit breaker | done | `outcomes.is_infra_failure`, `worker_loop` (restore row, 30 s back-off, stop after 3) |
| 5 Data repair | done | 12 rows back to `applied`, `review_status='needs_review'`, backup `applypilot.db.bak-pre-applied-restore-20260923` on the VM |
| 6/7 Tool error hints | done, **verified live** on a Greenhouse form | combobox lists real options (clears the typed filter first); trigger/click misses list labels/buttons |
| 8 Captcha no-sitekey | done | tool says "Output RESULT:CAPTCHA now", prompt matches |
| 9 `setTimeout` in run_code_unsafe | done | prompt |
| 10 Tracker | done | 6-file hash shared by `prompt.harness_version()` and the deploy script (test pins them equal); `Harness:` line in every prompt/trace; `DEPLOY_NOTE` column; log mirrored to the VM's `~/.applypilot/logs/agent_deploys.tsv` |
| Phase 3 prerequisite | done | `apply_runs` table, one row per attempt, incl. model, harness, `ab_arm` (unused until an A/B starts) |

**Measure next batch:** combobox error rate (baseline 45%), no-RESULT share
(baseline 28% on 09-22), and duplicate claims (baseline: 15 postings). Query
by harness: `SELECT harness, outcome, count(*) FROM apply_runs GROUP BY 1,2`.

## Where the waste is (ranked by $ and turns)

| # | Waste | Share | Lever |
|---|---|---|---|
| 1 | Duplicate claims then reaper kills (runs that produce nothing, sometimes after a real Submit) | **22% of spend**, 45/137 runs on 09-22 | Phase 1 bug fix |
| 2 | Workday volume: 178 calls/success, 33 snapshots, 43 s of fixed waits | 29% of spend, the biggest ATS cost | Phase 3 A/B (snapshot discipline, settle-wait) |
| 3 | Captcha grind: 21 traced captcha runs; 8 got an explicit unsolvable verdict and still ran a median 85 more calls | 13% of spend | Phase 2 (tool says "stop") + Phase 3 routing A/B |
| 4 | Blind tool failures: combobox 45% error, find_and_click 20%, no hint what *was* there | ~400 failed calls + recovery turns | Phase 2 tool diagnostics |
| 5 | Infra retry storms (browser down → instant requeue) | 183 runs in 30 min on 09-14; rows burned | Phase 1 circuit breaker |
| 6 | Partial cache misses mid-run | 21% of spend | **Not actionable** (provider-side); keep the provider pinned |
| 7 | Screenshots | ~2k tokens each, negligible | **Not a lever**; don't bother with `--image-responses omit` |

## Phase 1: correctness (now)

1. **Claim race.** `dedup.link()` commits only if it opened the transaction
   (`owns_tx = not conn.in_transaction`). Test: 8 threads × `acquire_job` on a
   temp DB, every returned URL must be distinct.
2. **`applied` is terminal.** Add `AND apply_status IS NOT 'applied'` to
   `mark_result`'s failure UPDATE. Test it.
3. **Reaper spares live runs.** A worker whose goose/MCP process is running
   (its cmdline carries `--cdp-endpoint=http://localhost:<port>`) is never
   reaped, whatever the DB says. Test the pure `orphans()` function.
4. **Infra circuit breaker.** Browser-infrastructure failures (`browser_*`,
   `no_browser_*`) put the row back to its prior status without counting an
   attempt. After 3 in a row the worker stops and logs why, instead of
   re-claiming instantly.
5. **Data repair.** Rows with an APPLIED transcript but a `failed` DB row get
   handled according to the user's decision (see questions).

## Phase 2: known-benefit tool/prompt fixes (now, after Phase 1)

6. `fill_searchable_combobox` / `human_fill_form` combobox path: on "fill
   failed", return the visible option texts (≤15). On "trigger not found",
   return the combobox/listbox labels on the page. Verify against a real live
   Greenhouse form (no submit), not just a compile.
7. `find_and_click` "could not locate": return the ≤10 closest visible
   button/link texts.
8. `handle_captcha` unsolvable messages ("turnstile … no sitekey", "detection
   failed", "budget exhausted") end with "→ output RESULT:CAPTCHA now". Today
   "turnstile … try manual fallback" invites the grind.
9. Prompt: `browser_run_code_unsafe` has no `require`/`setTimeout`; use
   `await page.waitForTimeout(ms)`. That's 19 errors in the sample.
10. **Tracker upgrade** (`logs/agent_deploys.tsv` already exists; the gaps are
    below):
    - The hash covers only 3 files. Widen it to the apply harness: prompt,
      mcp_tools, goose backend, launcher, chrome, known_quirks/issues.
    - The label is the last commit message, reused for 16 deploys in a row. Add a
      `DEPLOY_NOTE="…"` env var, and fall back to the `git diff --stat` of the
      harness files.
    - The hash never reaches the traces. Stamp `Harness: <hash>` into the prompt
      header, so every Langfuse trace carries it and ClickHouse can group on it.
    - Edits made directly on the VM bypass the log (the VM prompt.py mtime is
      09-21 17:32 with no log entry). The runtime hash in the prompt catches
      those too.

## Phase 3: later, needs an A/B test or more thought

Prerequisite: an attempt-level `apply_runs` table (url, session, worker,
start/end, result, reason, cost, turns, model, harness hash, arm). Assign arms
by `hash(url) % 2`, never by day. Judge on tool- and turn-level metrics first;
apply rate needs ~100 runs per arm to detect a 15-point difference.

| ID | Change | Metric | Guardrail |
|---|---|---|---|
| A1 | Workday/iCIMS snapshot discipline: prefer `snapshot_diff` (exists, used 20× total) and `browser_find` after the first snapshot | calls per applied Workday run (178) | apply rate |
| A2 | `wait_for_settle` tool (network-idle + DOM-stable, max N s) instead of fixed `browser_wait_for` sleeps | wall-clock, calls/run | success rate |
| A3 | Lever/iCIMS/Rippling routed straight to the home-fallback worker | captcha rate (40%) | **home IP health**, cap its share |
| A4 | Greenhouse deterministic presets for source/EEO questions via `human_fill_form` | combobox calls/run (10) | wrong-answer audit |
| A5 | Pre-flight captcha probe (HTTP fetch, vendor-script sniff) → skip or route | $ per captcha outcome | false-skip rate |
| A6 | Model challenger on a 20% live slice (not dry runs) | $ per applied, apply rate | stop at −10 pts |
| B1 | Resolve aggregator URLs in enrichment, not in the agent | aggregator $/applied (2× Greenhouse) | none needed |
| B2 | `upload_resume`: search iframes/shadow roots, click an "Attach" trigger first; add `apply-workers/current` to MCP allowed roots | upload error rate (12%, 48% fallback) | none needed |
| B3 | Mine killed/stuck runs from Langfuse back into `known_issues` (killed runs write nothing today) | repeat-failure rate per ATS | file bloat, needs dedup |

## New MCP tools?

**Build (generic, cross-ATS, measured frequency):**
- `wait_for_settle` (A2). Fixed sleeps are 797 calls; Workday alone waits 43 s per success.
- No new tool for comboboxes or clicking. **Fix the diagnostics of the existing
  ones (6, 7).** That's cheaper and doesn't grow the tool list.

**Don't build:**
- ATS-specific tools (a Workday wizard, Greenhouse EEO). Per the existing scope
  rule, those belong in `known_quirks` text.
- Anything whose value rests on one memorable transcript. Frequency across all
  traffic is the filter.

## What to avoid

- Capping turns (long runs are the successful ones).
- Treating screenshots or `--image-responses` as a cost fix (~2k tokens each).
- Widening home-proxy use without a cap (it's the one clean IP).
- Using dry runs for model/prompt comparisons (dry_run isn't enforced for every model).
- Comparing arms across days (batch mix and ATS mix swing daily).
- Deploying mid-run without checking KillMode, or shipping a dirty tree
  unintentionally. `deploy_to_vm.sh` rsyncs *everything*, including other WIP.
