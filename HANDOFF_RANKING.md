# Handoff — ranking rework, remaining work

Written 2026-09-07. Everything in "Done" is committed to the working tree
(uncommitted), tested, and verified in a browser. Everything in "Remaining" is
not started unless it says otherwise.

**Read `PLAN.md`'s final section ("Ranking rework: pay, prestige, and the
big-tech pin") first** — it has the full rationale. This file is only the
to-do list and the traps.

---

## The strategy all of this serves

Jakub is a senior graduating **May 2027**. He wants a **60/40 split**: 60% of
applications to new-grad roles (**$90k+**, New York strongly preferred), 40% to
Spring/Summer 2027 internships (**$30/hr+**, location flexible when pay or
relocation is good).

Two rules that drive most decisions here:

1. **Every FAANG/big-tech posting gets applied to regardless of skills fit.**
2. **Skills fit is nearly worthless as a signal** — most postings match most of
   his skills. It is a tiebreaker, never a gate.

"Terminal internship" means: the posting itself affirmatively welcomes a
candidate who has already graduated. That is what makes a **Summer 2027**
internship reachable at all (he graduates May 2027). **Spring 2027 is
trivially fine** — he's still enrolled through May.

---

## Done

| Area | What changed |
|---|---|
| Pay bug | `compute_desirability` now weights pay, prestige, location as three independent components, per lane (`new_grad_weights` / `internship_weights`). Previously pay was folded into location and **discarded entirely** for NYC postings. |
| Pay curves | `_pay_tier_score(salary, is_internship)` — continuous piecewise-linear, separate annual/hourly anchors. Unstated pay = neutral **5.0, never 0**. |
| Big-tech pin | New `company_tier` column (`tier1`/`adjacent`/NULL) from `config.TIER1_COMPANIES`/`TIER1_ADJACENT` + auto `prestige >= 9`. New `top` sort (now `DEFAULT_SORT`): tier → desirability → fit. `include_tier=true` exempts tiered rows from every `min_*` bar. |
| Resume variants | Grad-year axis ripped out (one grad date for everyone: `graduation_date` / `earliest_start_date` in settings). Track axis (swe/aiml/data) restored per Jakub's request -- `config.get_resume_paths(track)` + `resume_tracks` in settings, `resume_variant` DB column written again with the track name. |
| Sibling nuke | `_clear_terminal_flags_on_grad_date_mismatch` no longer disqualifies all siblings at a tier-listed or large (>8 postings) employer — marks them `unclear` instead. |
| Frontend | Gold chromatic sweep on big-tech names, live 60/40 `SplitCounter`, per-lane pay floors on the Priority panels, Big-tech/location/term filters, Big Tech preset chip, Settings → Ranking section. |
| CLI | `applypilot rescore-stale`, `applypilot recompute`. |

**State:** 228 tests pass, `ruff` clean, `npm run build` clean, verified in
browser. 41 files changed, **nothing committed** — the branch
(`goose-default-and-cleanup`) also carries unrelated pre-existing changes, so
check `git diff` before staging anything.

---

## Remaining work, in priority order

### 1. Finish the stale re-score — BLOCKED on rate limits

**Status: ran overnight, mostly failed.** 4,256 rows attempted, **only 989
scored**; 3,267 hit Gemini free-tier `429 Too Many Requests` starting ~36
minutes in and it never recovered.

**No data was corrupted** — `_write_score_results` keeps the existing score
when a row errors rather than writing the error sentinel. Re-running is safe
and idempotent.

**3,108 rows still have `term IS NULL`** (1,288 of them internships). Until
those are re-scored, `terminal_evidence_llm` is NULL for them, so the terminal
mechanism can't fire and Spring/Summer targeting falls back to reading titles.

The runner script is at
`/private/tmp/claude-501/-Users-jakubomylak-ApplyPilot/37c5cf8a-5b40-459e-9fb8-1af4f776034d/scratchpad/rescore.py`
(re-create it if the scratchpad is gone — it just calls
`run_scoring(stale_only=True)` after popping the OpenRouter env vars).

**Ask Jakub which provider to use before re-running.** The three options:

- `.env`'s configured `LLM_URL`/`LLM_MODEL` is `z-ai/glm-5.3-flash` on
  OpenRouter: **~15 s/call**, which is ~13 hours for what's left, but it did
  not rate-limit in testing.
- Gemini (`GEMINI_API_KEY`, `gemini-3.1-flash-lite`): sub-second per call but
  **free-tier limits cap it around ~1,000 rows/day**.
- A paid tier on either.

Whatever is chosen, add **backoff-and-resume** rather than burning the run:
`_SCORE_WORKERS = 25` in `scoring/scorer.py:35` is almost certainly what
tripped the limit. Drop it to ~5 and re-run `applypilot rescore-stale` in
chunks (`--limit`) across days if staying on the free tier.

After any re-score, run `applypilot recompute` (free, no LLM calls).

### 2. Loosen the terminal gates — DECIDED, not implemented

`compute_terminal_internships` (`scoring/scorer.py`) gates on `terminal_min_fit: 6`
and `terminal_min_prestige: 6`. **This contradicts the rest of the rework** —
we demoted fit everywhere else and pinned big tech above every bar, but the
terminal flag still uses both as hard gates.

Measured on the current data: of 122 internships with LLM terminal evidence,
**33 are dropped for prestige < 6** and **4 for fit < 6**. Those are postings
that genuinely welcome a graduated candidate and are simply hidden.

Jakub was asked and has **not answered yet** — confirm the shape before
building. The recommendation is: exempt `company_tier IS NOT NULL` rows from
both gates (mirroring `include_tier` in `web/queries.py`), and drop
`terminal_min_fit` to 0 since fit is no longer a gate anywhere else.

### 3. Fix 4 verified false-positive terminal flags

I read the descriptions of all 73 flagged terminal internships (the count is
**84** now after the partial re-score — the extra 11 have not been audited).
**66 of 73 carried an explicit escape clause and are correct.** Verified good
examples: Y-12 ("eligible up to two years post-graduation"), Prophet Security
("seeking graduating seniors"), Palantir ("pursuing **or received** a degree"),
Genentech ("attained a Bachelor's, **not currently enrolled**"), UPS ("or
graduated from within the last 24 months"), Booz Allen ("Bachelor's by Winter
or **Spring 2027**").

The four that are wrong:

| Posting | Actual text | Problem |
|---|---|---|
| **Verkada** ×2 (Backend, Security SWE Intern 2027) | "**Actively pursuing** a Bachelor's or Master's... graduating by June 2028" | Requires ongoing enrollment. This is the same company the codebase cites as its canonical `grad_date_mismatch` case. |
| **Notion** (SWE Intern Summer 2027) | "Pursuing a bachelor's... **Must graduate before Summer 2028**" | A ceiling on graduation date, not permission to have already graduated. |
| **SAP iXp Full-Stack AI Developer** | "**Master's degree** in computer science... required" | Should be `eligible='no'` outright — this is an eligibility miss, not just a bad terminal flag. |

Two more are imperfect but harmless — **TikTok Research Engineer
(Monetization)** is Master's-targeted (same eligibility miss; its terminal
wording is actually fine), and **Sanofi 2027 Spring Co-op** says "enrolled
throughout the co-op" but it's a *Spring* term, so he is enrolled throughout
and can apply anyway.

**Fix:** add to `TERMINAL_EXCLUDE_RE` (`scoring/scorer.py`) — `actively
pursuing`, and a `must graduate (before|by) <date>` pattern that isn't
accompanied by an "or completed/graduated" escape. Separately, strengthen the
scoring prompt's ELIGIBILITY CHECK so a required Master's is caught (it
already lists Master's as a disqualifier — these two slipped through, so it
needs a sharper example, not a new rule).

Add regression tests using the real phrasings above; `tests/test_terminal_internship.py`
is the existing home for this.

### 4. Smaller things

- **`applypilot status`** (`cli.py:_build_status_renderables`) still reports
  `score_distribution_by_type` and nothing about tiers or the 60/40 split.
  Worth a tier row.
- **`profile.json` contains a plaintext `personal.password`.** Pre-existing,
  not touched, but Jakub should know.
- The `resume_variant` DB column is alive again, storing the swe/aiml/data
  track name (see "Resume variants" above) -- no longer dead, no migration
  needed since the column already existed.
- `enriched_only.db` (28 MB) sits at the repo root and is not gitignored.

---

## Traps — things that will waste your time

1. **`_bootstrap()` is called per-command in `cli.py`, not in the Typer
   callback.** A new subcommand that forgets it gets "No LLM provider
   configured" even though `~/.applypilot/.env` is populated. This cost a
   debugging cycle already.
2. **`.env` overrides Gemini.** `LLM_URL` + `LLM_MODEL` point at OpenRouter, so
   `get_client()` returns GLM even with `GEMINI_API_KEY` set. Pop
   `LLM_URL`/`LLM_MODEL`/`LLM_API_KEY`/`OPENROUTER_API_KEY` from `os.environ`
   after `load_env()` to force Gemini.
3. **`score_job(resume_text, job)`** — resume first. The reversed order fails
   with a confusing `'str' object has no attribute 'get'`.
4. **`parse_pay_range` wants the DB's normalized format** (`$45-$60/hr`,
   `$120,000 - $150,000/yr`). Prose like `"$120,000 a year"` returns `None`, so
   ad-hoc test strings silently score a neutral 5.0.
5. **`/api/launch` spends real money.** Always `dry_run: true` when testing.
   The web UI launches `--queued <batch>`, which deliberately bypasses
   min_score, pay floor, eligibility and age decay — the human selection *is*
   the filter.
6. **The launcher must stay a subprocess.** `launcher.main()` installs a
   process-global SIGINT handler and `atexit` hooks; importing it into the
   server takes the server down with it.
7. **Each day section is an independent table** with its own sort/filter/page
   state. Not one global table with a date column. Jakub was explicit.
8. **Rebuild the frontend after any `web/src` change**: `cd web && npm run
   build` writes into `src/applypilot/web/static/`. A stale `serve` process
   also serves stale assets — kill and restart it (`lsof -ti:8420`).
9. **`compute_desirability` and `compute_company_tiers` are pure arithmetic.**
   Re-tuning weights or editing the company list never needs a re-score — just
   `applypilot recompute`.

---

## How to verify you haven't broken it

```bash
python3 -m pytest tests/ -q          # 228 passing
python3 -m ruff check src/ tests/
cd web && npm run build && cd ..
```

The pay bug has a specific regression query — at fixed prestige, desirability
must still vary with pay:

```sql
SELECT company_prestige, ROUND(pay_max_hourly/10)*10 pay_hr, COUNT(*),
       GROUP_CONCAT(DISTINCT desirability_score)
FROM jobs WHERE job_type='new_grad' AND location LIKE '%New York%'
  AND pay_max_hourly>0 AND company_prestige=7 GROUP BY 1,2 ORDER BY 2;
```

Before the fix every bucket returned `8.8`. It should now climb ~6.1 → ~9.0.
`tests/test_ranking.py` pins this and the tier behaviour.

Browser check: `applypilot serve`, open `127.0.0.1:8420` — both Priority lanes
should lead with gold-animated big-tech names, the header should read
"prestige 6+ · $30/hr+ (or $90k+) · big tech always", and ticking rows across
both lanes should move the 60/40 counter (amber past ±10 points).
