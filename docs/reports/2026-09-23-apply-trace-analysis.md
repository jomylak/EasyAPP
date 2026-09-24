# Apply-run trace analysis — 2026-09-11 → 2026-09-23

Sources (all on the Oracle VM): Langfuse ClickHouse (562 traces, 40.4k
observations, 20.7k tool calls), `worker-*.log` + `goose_*.txt` transcripts
(595 runs with outcomes), `goose_generations.jsonl`, jobs DB. Runs were joined
to traces via goose `session.id`. Cost is computed from Langfuse token usage at
mimo-v2.5 rates (prompt $0.14/M, cache $0.0028/M, completion $0.28/M).

"Production" below = the 391 goose + `xiaomi/mimo-v2.5` runs; 205 of those
have a trace (Langfuse coverage starts 09-13).

---

## 0. TL;DR — the top 5, in order

| # | Finding | Impact | Fix size |
|---|---------|--------|----------|
| 1 | **Claim race: several workers apply to the same job at once.** `dedup.link()` calls `conn.commit()` (`dedup.py:316`) *inside* `acquire_job`'s `BEGIN IMMEDIATE`, so the lock drops before the row is flipped to `in_progress`. Reproduced on a DB copy: 8 concurrent `acquire_job` calls → the same row handed to 2–3 workers, every trial. | ≥1 confirmed **double submission** (Roku, 21:33:37 + 21:35:17 on 09-22). 5 workers on one D. E. Shaw posting. Drives most of finding #2. | ~3 lines |
| 2 | **The orphan reaper kills the extra copies mid-run** because only one worker owns the row. 28% of 09-22 runs (38/137) ended with no RESULT. Almost all of them hit `ECONNREFUSED :922x` right after a `reap worker-N chrome` line in `journalctl -u orphan-reaper`. | 22% of traced spend ($1.34 / $6.07) went on runs that never produced a result. | falls out of #1 |
| 3 | **A later failure can overwrite `applied`.** The failure branch of `mark_result` has no `apply_status != 'applied'` guard. The Roku row now says `failed/page_error`, so it's eligible for a *third* submission. 14 application URLs have an APPLIED transcript but a `failed` DB row (list in §6). | Integrity + duplicate-apply risk | 1 line |
| 4 | **Harness change tracking is thin.** *(Correction: `logs/agent_deploys.tsv`, written by `deploy_to_vm.sh`, does exist; this report originally missed it.)* But it hashes only 3 files, labels 16 consecutive deploys with the same commit subject, is local-only (gitignored), misses edits made directly on the VM, and nothing ties a Langfuse trace to a hash. | Blocks clean A/B tests | small (§5; done, see plan) |
| 5 | **`fill_searchable_combobox` fails 45% of the time** (243/542), and the error doesn't say which options existed. The model then guesses and retries. It's the #1 tool on Greenhouse (10 calls per applied run). | Turns on Greenhouse (our biggest ATS) | small |

---

## 1. Outcomes

### Production (mimo), n=391

| Result | Runs | % | Median tool calls | Median wall-clock |
|---|---|---|---|---|
| APPLIED | 196 | 50% | 90 | 7.4 min |
| CAPTCHA | 49 | 13% | 98 | 11.3 min |
| FAILED | 49 | 13% | 47 | 3.0 min |
| EXPIRED | 29 | 7% | 2 | 0.3 min |
| no RESULT / no transcript | 68 | 17% | 52 | 4.6 min |

FAILED reasons: login_issue 9, page_error 7, sso_required 6,
not_eligible_location 3, captcha-ish 3, browser_runtime_down 3, ashby spam
detection 2, WAF block 2, pay_below_floor 2, and a long tail of single cases.

### Per day

| Day | Runs | Applied | Captcha | No result | Notes |
|---|---|---|---|---|---|
| 09-10 | 62 | 31 | 7 | 5 | pre-Langfuse |
| 09-11 | 124 | 71 | 21 | 8 | pre-Langfuse |
| 09-14 | 252 | 30 | 1 | 66 | **includes the GLM/DeepSeek retry storm (§2.3)** |
| 09-22 | 137 | 56 | 17 | **45** | claim race + reaper (§2.1) |

### By ATS (production, apply-rate excludes EXPIRED)

| ATS | Runs | Applied | Apply rate | Captcha | No result | Median calls/applied | Median $/applied |
|---|---|---|---|---|---|---|---|
| Greenhouse | 86 | 58 | 72% | 6 | 11 | 90 | $0.021 |
| aggregator (unresolved) | 66 | 36 | 58% | 6 | 7 | 106 | $0.047 |
| Workday | 49 | 23 | 52% | 3 | 11 | **178** | **$0.072** |
| Ashby | 44 | 25 | 66% | 6 | 5 | **18** | **$0.009** |
| custom careers | 28 | 12 | 46% | 1 | 8 | 117 | $0.020 |
| iCIMS | 20 | 5 | **26%** | 6 | 4 | 115 | $0.052 |
| Oracle HCM | 16 | 8 | 53% | 4 | 1 | 91 | $0.045 |
| Lever | 12 | 1 | **8%** | **6** | 3 | 122 | — |
| SAP SuccessFactors | 11 | 3 | 38% | 1 | 2 | 78 | $0.065 |
| Rippling | 6 | 0 | 0% | 3 | 3 | — | — |
| BambooHR | 5 | 0 | 0% | 1 | 3 | — | — |

- **Ashby is the benchmark.** 18 calls and under a cent per success.
- **Workday takes 29% of traced spend** ($1.73). An applied Workday run averages
  43 clicks, 33 snapshots, 24 finds and ~43 s of `browser_wait_for`.
- **Lever / iCIMS / Rippling are captcha-bound.** 15 of 38 runs ended on a
  captcha, and those runs grind for 88 calls on average.
- **Aggregator-unresolved URLs** cost roughly 2× Greenhouse per success. Resolving
  them to the real ATS upstream (enrichment) is cheaper than doing it in the agent.

---

## 2. What's driving errors

### 2.1 Claim race → duplicate workers → reaper kills (P0)

Mechanism, verified end to end:

1. `acquire_job` runs `BEGIN IMMEDIATE`, selects a queued row, then calls
   `_live_duplicate_already_committed` → `dedup.link(conn, url)`.
2. `dedup.link` ends with an unconditional `conn.commit()`. That ends the claim
   transaction early. SQL trace from the repro:
   `T6 BEGIN IMMEDIATE … T6 UPDATE … company_normalized … T6 COMMIT … T6 BEGIN`.
3. Other workers are blocked on `BEGIN IMMEDIATE`. They now get the lock, see
   the row still `queued`, and claim it too. The same gap lets the
   company-lock check pass for the duplicates.
4. Only the last writer's `agent_id` sticks. `orphan_reaper.py` (every 2 min) sees
   Chrome for workers with no `in_progress` row older than 180 s and kills it.
   From the journal: `04:34:52 reap worker-1/3/0/5/2`,
   `08:24:53 reap worker-0/3`, `09:53–09:57 reap worker-2/7/5/0`,
   `19:13–19:15 reap worker-5/7/2/0`. These line up exactly with the NO_RESULT
   runs' `ECONNREFUSED` endings.

Evidence of harm:

- 15 postings were worked by >1 worker concurrently (215 runs total, most in the
  09-14 storm below).
- **Roku**: worker 5 submitted (21:33:37, `ok: no errors found`) and worker 0
  submitted again (21:35:17) before it was reaped. That's a real double apply.
- **Veeam**: the killed worker clicked "Submit application" and got a validation
  error before it died. It probably didn't go through, but the row is back to NULL
  and will be retried.
- D. E. Shaw row `…6a1d9614` has `apply_attempts=4` from one second of claims.

Fix: have `link()` commit only when it opened the transaction itself. Minimal
version:

```python
def link(conn, url):
    owns_tx = not conn.in_transaction
    ...
    if owns_tx:
        conn.commit()
```

Re-run the concurrent `acquire_job` repro (8 threads on a DB copy) as the check.

### 2.2 `applied` isn't terminal in `mark_result`

The failure branch has no guard, so a killed or duplicate sibling that finishes
later overwrites a real success with `failed`. Add
`AND apply_status != 'applied'` to that UPDATE. §6 lists the rows to review by hand.

### 2.3 Retry storm on infra failure (09-14 04:28–04:57)

A GLM-5.3-flash / DeepSeek trial ran with the browser down: 183 GLM runs, 100%
tool error rate, a median of 5 calls each. `browser_down`, `browser_unreachable`
and `browser_runtime_*` failures are non-permanent, so the same 4 rows were
re-claimed about 180 times in 30 minutes (Stripe ×65, Brunswick ×45, Tebra ×39,
HPE ×35). This also inflated those rows' `apply_attempts`.
Fix: an infra-class failure should not count as a job attempt and should not
re-queue instantly. The worker should restart Chrome, and after N in a row it
should stop.

### 2.4 Tool error rates (production traces, 14.8k calls)

| Tool | Calls | Error % | Top errors |
|---|---|---|---|
| applytools `fill_searchable_combobox` | 542 | **45%** | "combobox fill failed" with no detail (132), locator timeout (32), "could not locate trigger" (31). Worst fields: How did you hear (24), Veteran status (14), Location (City) (10), Gender, Field of study, Degree, School |
| playwright `browser_file_upload` | 33 | 48% | "File access denied: …/apply-workers/current/…" (8), no modal state (5) |
| applytools `handle_captcha` | 161 | 28% | turnstile with no sitekey (18), hcaptcha hard stop (8), driver closed (6), capsolver 400 (3) |
| applytools `find_and_click` | 671 | 20% | no clickable element (75), click timeout (47) |
| applytools `human_fill_form` | 227 | 19% | "not an input" (12), field not found (16+) |
| playwright `browser_run_code_unsafe` | 563 | 19% | click timeout (19), **`require is not defined` (10), `setTimeout is not defined` (9)**, modal state (7) |
| playwright `browser_select_option` | 87 | 18% | |
| playwright `browser_navigate` | 460 | 17% | ECONNREFUSED from reaped Chrome (29), proxy tunnel / timeouts (17) |
| applytools `upload_resume` | 187 | 12% | "no `<input type=file>` found" (19) |
| playwright `browser_click` | 3134 | 7% | callTool 30 s timeout (104), stale ref "not found in snapshot" (84) |

Only 121 of 14.8k calls were exact back-to-back repeats, and just 91 calls were
made after a browser died (median 4 per trace). **The agent isn't looping
blindly.** The waste comes from tools that fail without saying why.

### 2.5 Other

- `known_quirks` exists for only 4 ATSs (greenhouse, workable, workday,
  aggregator). Lever, iCIMS, Rippling, SuccessFactors and Oracle HCM have
  `known_issues` but no verified quirks. Killed runs still write nothing (known gap).
- Startup: `npm ENOTEMPTY` on `@playwright/mcp` still fired once on
  09-22 04:31 despite the pinned-version pre-warm, plus 2–4 "Failed to start
  extension" cases. These are rare but silent: goose continues with no
  browser tools.

---

## 3. What's costly

Traced production spend: **$6.07 across 205 runs**, i.e. **$0.065 per applied job**
once failures are counted ($0.021 median for the successful run alone).

**By outcome:** applied 53%, **no result 22%** (+3% no transcript),
captcha 13%, failed 7%, expired 1.5%.

**By token type:** fresh prompt 48%, cache read 44%, output 9%. The cache hit rate
is 98.3% of tokens.

- Fresh-token cost breaks down into:
  - the first turn (a ~25k-token prompt, ~11k of it uncached): $0.41 (7% of spend)
  - partial cache misses mid-run: 2.6% of turns re-bill about 21% of the prompt fresh, $1.25 (21% of spend)
  - the unavoidable per-turn delta (new tool output), the rest
- Miss rate rises with the gap between turns (2.6% under 10 s → 11% at 60–120 s). This is
  provider-side cache eviction, so there's little to do beyond keeping the provider pinned.

**By tool (share of output bytes pushed into context):** snapshot 26%,
screenshot 56%. **Correction to earlier notes:** for mimo a screenshot adds
only **~2k prompt tokens** on the next turn (it's billed as an image, not as base64
text). The 176 screenshots are negligible cost, so `--image-responses omit` is not
a cost lever. Snapshots are: each adds a median ~950 tokens that then ride along
in cache for the rest of the run, and Workday averages 33 per success.

**Don't cap turns.** Consistent with the earlier cost-model finding, long runs are
the successful ones. The levers are removing dead spend (#1–#3) and cutting
calls per field (combobox, snapshots).

---

## 4. Recommendations

### 4a. Known-benefit fixes (just ship them)

1. **Fix the claim race** (`dedup.link` commits only if it owns the
   transaction). Expected: most of the 22% no-result spend disappears, and
   duplicate submissions stop.
2. **Make `applied` terminal in `mark_result`.** Then hand-review the §6 rows and
   restore any real successes.
3. **Reaper defense in depth.** Treat a worker as live if its goose process is
   alive (the `_goose_procs` pids, or a heartbeat file), not only if it holds a DB
   row. That way a future bookkeeping bug can't turn into a mid-submit kill.
4. **Infra-failure circuit breaker (§2.3).** Don't count the attempt, don't
   re-queue instantly, restart Chrome, and stop the worker after 3 in a row.
5. **Make `fill_searchable_combobox` failures informative.** On "fill failed",
   return the visible option texts (first ~15). On "could not locate trigger",
   return the combobox labels present on the page. Same idea for
   `find_and_click`: return the closest clickable texts.
6. **Captcha hard stop.** When `handle_captcha` returns an unsolvable verdict
   (turnstile with no sitekey, hcaptcha already attempted, budget exhausted),
   the prompt should mean *emit RESULT:CAPTCHA now*. *(Corrected figure:* 8 of the
   21 traced captcha runs got such a verdict and still made a median 85 more calls.
   The earlier "84%" counted benign "no captcha detected" replies.)
7. **Prompt one-liners** for `browser_run_code_unsafe`: no `require`, use
   `await page.waitForTimeout(ms)` instead of `setTimeout`. That removes 19 errors.
8. **File upload.** `upload_resume` should also search iframes and open shadow roots,
   and try clicking an "Attach/Upload" trigger before giving up. Add the
   `apply-workers/current` path to Playwright MCP's allowed roots so the
   `browser_file_upload` fallback stops getting "File access denied".
9. **Resolve aggregator URLs upstream**, before the agent spends turns doing it.

### 4b. A/B tests (after §5 exists)

Volume is about 100–140 runs on a batch day. At that size an arm needs ~100
runs to see a 15-point apply-rate difference. **Prefer tool- and turn-level
metrics**, which have hundreds of samples per day. Assign arms by
`hash(url) % 2`, never by day, and record the arm on the attempt row.

| Test | Arm B | Primary metric | Guardrail |
|---|---|---|---|
| T1 Snapshot discipline (Workday, iCIMS) | Prompt: after the first snapshot, use `snapshot_diff` / `browser_find`; full snapshot only after navigation or a stale-ref error | calls per applied Workday run (now 178) | apply rate |
| T2 Captcha-prone ATS routing | Lever, iCIMS and Rippling go straight to the home-fallback worker | captcha rate on those ATSs (now 40%) | **home IP health (it's scarce, see the IP-reputation note), cap the share** |
| T3 Greenhouse EEO/source defaults | Deterministic answers for "How did you hear", Veteran, Gender, Disability, Hispanic via `human_fill_form` presets | combobox calls per Greenhouse run (now 10) | wrong-answer spot check |
| T4 Model challenger | e.g. GLM-5.3-flash vs mimo on a 20% live slice, **not dry runs** (dry_run isn't trustworthy) | apply rate, $ per applied | stop early if apply rate drops >10 pts |
| T5 Pre-flight captcha probe | Plain page fetch before launching the agent; skip or route if a hard captcha vendor script is present | $ per captcha outcome | false-skip rate |

Past model trials (GLM 09-14, DeepSeek 09-14, nex/gemini-lite 09-16–21) can't
be compared to mimo. GLM ran against a dead browser, DeepSeek hit 12 expired out of 21,
and the rest were quicktests with no outcome recorded. Don't draw conclusions from them.

---

## 5. Change tracking (doesn't exist yet)

Needed so every run can be attributed to the exact prompt, tools and quirks it
ran with. Minimum setup:

1. **`harness_version`**: a short hash of `apply/prompt.py`,
   `apply/mcp_tools/server.py`, `apply/backends/goose.py` and the
   `config/known_quirks|known_issues` dirs, computed at run start.
2. **Put it in the prompt header** (`Harness: <hash>`). The trace input already stores
   the full prompt, so this makes it queryable in ClickHouse with no exporter
   work. `extract(toString(input), 'Harness: (\\w+)')`
3. **An attempt-level `apply_runs` table**, one INSERT in `mark_result`:
   url, worker, goose session_id, started/ended, result, reason, cost, turns,
   model, harness_version, ab_arm. The `jobs` row keeps only the *last* attempt,
   so this report had to rebuild attempt history by parsing worker logs.
4. **`deploy_to_vm.sh` appends a line** to `~/.applypilot/harness_changes.log`:
   timestamp, `git rev-parse --short HEAD` + dirty flag, harness_version, and
   `git diff --stat` of the harness files. That's the human-readable changelog.

With 1–4 in place, "did change X help?" becomes a GROUP BY on
`harness_version`.

---

## 6. Rows to review by hand

APPLIED transcript, but the DB row is now `failed` (possible overwrite by a
later attempt; verify in Gmail before re-queuing any of them):
Axpo US, Slalom, Roblox, American Express (Oracle HCM), OMERS, ConductorAI,
PepsiCo (iCIMS), Capgemini, Epic Games, State Farm, Zappos/Amazon, Roku.

Another 23 had APPLIED transcripts whose application URL no longer matches any DB
row (URL re-resolution or dedup merges), so no conclusion there.

---

## Method notes / caveats

- The trace ↔ run join uses the goose session id printed in `worker-N.log`. 408/595
  runs matched, and every run after 09-13 matched.
- Langfuse's final generation span rarely carries output text (the stream is cut
  at process exit), so RESULT was taken from the transcripts. Only 47 traces
  have it in-trace.
- ATS labels come from the jobs DB where the URL matched, otherwise from the URL
  hostname.
- The analysis scripts (ClickHouse export SQL, `parse_runs.py`, pandas joins)
  are in this session's scratchpad. They can be promoted to `scripts/` if this
  becomes a recurring report.
