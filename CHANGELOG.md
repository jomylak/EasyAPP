# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-09-04

### Added
- **`applypilot serve`: a local web UI for choosing what to apply to.** With
  AI/ML and the other tags enabled the pipeline produces several hundred
  postings a day, which is more than the ranked queue's "apply to the top of
  the list" model can spend responsibly. The UI groups jobs by the day they
  were posted, gives each day its own independently filtered and sorted table,
  and applies only to the rows you tick. Bound to `127.0.0.1` with no `--host`
  option: it serves your resumes and drives Chrome profiles holding your
  logged-in sessions.
- **`applypilot apply --queued <batch>`.** Drains a user-selected batch in the
  order it was selected, deliberately ignoring `--min-score`, the pay floor,
  eligibility and age decay -- when a person has picked the jobs, their
  selection is the ranking, and re-filtering it would silently drop rows they
  explicitly chose.
- **Cost estimation before you spend (`applypilot.costs`).** The backends
  always reported what a run cost afterwards; nothing predicted it beforehand.
  Estimates come from the median of comparable past runs, narrowing to the same
  ATS once there are enough of them, and always report how many observations
  they rest on so a guess is never presented as a measurement.
- **Indexes on `jobs`.** The table had none beyond the URL primary key, which
  was survivable when every reader was a CLI doing one full pass per stage and
  is not when a browser reads a filtered, sorted slice of 5,000+ rows per day
  section.

### Fixed
- **A single unusable job no longer strands every job behind it.**
  `acquire_job` returned `None` when it skipped a manual-ATS posting, and
  `worker_loop` reads `None` as "queue empty" and stops. One such posting
  partway down a run ended the whole run early. Unusable rows are now recorded
  with a terminal status and the drain continues past them.
- **Stale apply locks are released.** `apply_status = 'in_progress'` is a lock
  with no heartbeat, so a killed launcher left its claimed rows unreachable to
  every future run. `applypilot serve` sweeps them at startup.
- **The dashboard's Company column showed the job board, not the employer.**
  Both backends reported `job["site"]` ("Intern List - SWE") as the company.
  They now report the `company` column, falling back to the board only when
  scoring has not filled it in yet.
- **Goose apply backend, now the default.** `applypilot apply` drives the
  browser with the Goose CLI on a cheap OpenRouter model (default
  `xiaomi/mimo-v2.5`, ~$0.05/application) instead of Claude subscription
  quota. Same prompt, same Playwright + Gmail MCP servers, same Chrome, same
  `RESULT:` vocabulary and known-quirks cache as the Claude path -- only the
  model behind the loop differs, so completion rates stay comparable.
  Promoted from `scripts/goose_quicktest.sh`, which had already proven the
  approach against real ATS forms.
- **Automatic Claude fallback.** A job Goose gives up on is retried once on
  the Claude backend, but only for failures meaning the *driver* lost the
  thread (`stuck`, `timeout`, `page_error`, `no_result_line`, `unknown` --
  `outcomes.FALLBACK_REASONS`). Expired, already-applied, and SSO-walled
  postings are just as dead for the stronger model, so they are never
  retried. Chrome is restarted between attempts so the retry starts clean.
  Configure with `apply_fallback_backend`, or `--fallback none` per run.
- `--fallback` CLI flag; `goose_model`, `goose_provider`, `goose_max_turns`,
  `goose_max_tool_repetitions`, `goose_timeout` and `goose_writes_quirks`
  settings.
- Wall-clock timeout on apply runs (`goose_timeout`, default 2400s).
  `--max-turns` bounds turns, not time, and a cheap model can sit in a slow
  tool call indefinitely.
- `tests/test_goose_backend.py`: 37 tests covering command construction,
  stream-json parsing, and fallback routing.

### Changed
- `doctor` reports which backend is primary vs. fallback, and checks Goose and
  `OPENROUTER_API_KEY` alongside the Claude CLI.
- Tier 3 (auto-apply) is unlocked by *either* backend. It previously required
  the Claude CLI specifically, which would have reported tier 2 for a working
  Goose-only install -- the new default.
- `.env.example` rewritten: `OPENROUTER_API_KEY` promoted to required,
  `LLM_API_KEY` documented, provider precedence spelled out.
- `SETUP.md` added for from-scratch setup; linked from the README.

### Fixed
- `doctor` reported the LLM provider as Gemini whenever `GEMINI_API_KEY` was
  set, even when `LLM_URL` was also set and `llm._detect_provider()` was
  therefore routing every call elsewhere. It now mirrors the real precedence
  and warns when `LLM_URL` shadows a key.

### Removed
- **Skyvern backend, entirely.** Along with `apply/fileserver.py`,
  `apply/verification.py`, `apply/gmail.py` (all Skyvern-only),
  `prompt.build_skyvern_goal()`, `outcomes.ERROR_CODE_MAPPING`, the
  `skyvern_*`/`verification_*` settings, and `docs/skyvern-backend.md`. Goose
  covers the same "don't burn Claude quota" case with no server to run.
  `SKYVERN_*` env vars are now ignored.

## [2026-09-03] - 2026-09-01 to 2026-09-03

Two sessions' worth of changes across the apply agent, discovery, and
enrichment pipeline. See `HANDOFF.md` for full context, open items, and
known limitations -- this section is the terse historical record.

### Added
- Housing/relocation support now counts toward internship desirability:
  `offers_housing()` plus a flat `housing_bonus` (0.5) applied outside the
  weighted components, reaching all 250 of 1101 internships that offer it.
  Explicit refusals ("No Corporate Housing Provided") score nothing.
  Detection only -- no dollar parsing: an hourly-conversion version was built
  first and measured, and it credited 249 jobs but changed only 56 scores,
  always by exactly +1.00, because the pay tier is a step function that threw
  the precision away. It also dropped 36 jobs in preferred metros, where
  _location_desirability returns before the pay tier is consulted, so a San
  Francisco internship with a stipend got nothing in the city where housing
  matters most. Reading no numbers also removes the whole class of
  wage-mistaken-for-stipend bugs
- `terminal_evidence()` + `compute_terminal_internships()` in `scoring/scorer.py`:
  deterministic, no LLM, derived from stored columns like `compute_desirability`.
  Matches explicit acceptance ("or recently graduated", "recently completed a
  degree", upper bounds like "must graduate before December 2027") and vetoes
  graduation *windows* that exclude the candidate ("December 2027 and beyond",
  "or later", "must continue enrollment"). 100% precision against the audited
  sample
- `terminal_min_fit` (9) and `terminal_min_desirability` (6.0) settings
- Deterministic resume-track routing (`scoring/router.py`): picks one of three
  hand-authored LaTeX resume variants (`swe` / `aiml` / `data`) per job from
  the title, then the scorer's `keywords`, then the description -- no LLM call,
  so the tailor stage stays free across thousands of jobs and a misroute can be
  predicted by reading the title. Precedence is AI/ML > SWE > Data; SWE is also
  the fallback for the ~33% of titles matching no track
- Two-tier routing vocabulary: terms common to all three tracks (Python, Git,
  AWS, SQL) are excluded outright, and bare "AI"/"ML"/"dashboard" are decisive
  only in a *title*. Bare "AI" fired 38 times across the tailorable set --
  more than every genuine AI term combined -- because postings list "AI tools"
  as boilerplate, which was routing "IoT Engineer" to the AI/ML resume
- Six resume variants in `settings.json` keyed `{track}_{gradyear}`, from three
  maintained LaTeX sources compiled twice each with a different `\graddate`
- `tests/` with 37 cases covering routing precedence, the weak-term tier, the
  field cascade, and variant composition
- Per-ATS known-quirks cache (`config/known_quirks/<platform>.md`): verified
  widget-handling fallbacks loaded into the apply prompt, self-reported by
  the agent via a `QUIRK:` line and written back only for trusted models
  (Sonnet/Opus) on a successful run
- `DATE FIELD PROTOCOL` in the apply prompt: classify a date widget into one
  of three confirmed patterns (typeable / spinbutton-triplet / calendar-
  popup-only) before acting
- ATS resolution at enrichment time: `resolve_original_job_url()` clicks
  through Jobright's real Apply flow to the actual employer URL, requires a
  signed-in Jobright session (`ENRICHMENT_PROFILE_DIR`, a persistent Chrome
  profile separate from the apply-workers' profiles)
- NewGrad Jobs discovery: bespoke Airtable Button-field scraper (the site
  embeds an Airtable grid, not a Jobright iframe like Intern List)
- Job queue age-decay: small priority penalty at pick-time based on
  `posted_date`/`discovered_at`, tunable via `job_age_decay_per_day`
  (queue-ordering only, never rewrites `fit_score`)
- `scripts/goose_quicktest.sh`: paste a job URL + OpenRouter model, get a
  real ApplyPilot prompt run through Goose against the same Playwright MCP
  server the Claude Code backend uses, with `email_override`/
  `password_override` support for same-job regression testing
- Per-resume-variant `start_date` in `settings.json` (alongside `grad_date`)
  -- the gap between graduating and being available isn't a fixed offset
  across variants, so it's configured explicitly per variant, not computed
- `RESULT:FAILED:grad_date_mismatch` + automatic resume-variant swap on that
  failure, so a retry uses the corrected resume instead of repeating the
  same mismatch
- Two-number job scoring: `fit_score` stays a pure skill match, and a new
  `desirability_score` (company prestige + pay + location, weighted, no LLM
  call) captures how much the candidate actually wants the role. The apply
  queue orders on a configurable blend of the two (`fit_weight` /
  `desirability_weight` in settings.json), so ordering can be re-tuned without
  re-scoring anything
- Prestige override on the apply queue (`prestige_override_tiers`, applied by
  `database.fit_gate_sql()` to both the tailor stage and `acquire_job`): a
  strong enough employer qualifies below the normal fit bar, on the trade that
  one application's cost is bounded while the upside isn't. Safe only because
  the eligibility gate independently drops roles that can't be filled
- Roles requiring 3+ years of professional experience (internships excluded)
  are a hard disqualifier; 1-2 years is not, since those asks are often soft
- Hard eligibility gate at scoring time (`eligible` / `eligibility_reason`):
  rejects Master's/PhD-only, freshman/sophomore-only, non-US, and
  clearance-required postings before they reach an apply run. Graduation
  timing is explicitly NOT a disqualifier -- the resume-variant swap already
  handles it -- and work authorization is passed in from the profile as fact
  rather than left for the model to infer
- `company` and `company_prestige` columns, extracted by the scorer in its
  existing call. Previously the scorer was passed `site` ("Intern List - SWE")
  as COMPANY, so it never saw the employer at all
- `parse_pay_range()` / `pay_below_floor()`: normalize discovery's `salary`
  strings (`$23-$43/hr`, `$6k-$11k/mon`, `$43k-$74k/yr`) to an hourly range.
  The floor test is on the range MAXIMUM, so a band that straddles the floor
  isn't rejected

### Fixed
- Housing detection needs two guards that each caught a real posting: word
  boundaries (PepsiCo's "data warehousing" contains "housing") and a benefit
  word alongside the noun ("Freddie Mac is a housing finance company" is not
  an offer)
- `is_terminal_internship` was flagging jobs on *absence* of evidence. It keyed
  off `requires_returning_student == "no"`, but the scoring prompt tells the
  model to answer "no" when "the posting states no graduation timing
  requirement at all", and the parser collapses every non-"yes" answer --
  including a malformed one -- to "no". An LLM audit of 40 of the 94 flagged
  jobs found **77.5% flagged on silence, 20% with real evidence, 2.5%
  outright wrong** (PepsiCo: "graduate ... within one (1) year of internship
  completion", a window a May-2027 grad falls before). The flag now requires
  an explicit statement (`terminal_evidence()`), and 94 -> 4
- The flag is an absolute first sort key in `acquire_job`, so it had taken
  **93 of the top 100 queue slots** -- a fit-9/desirability-2.0 role at an
  unknown company outranked fit-10/desirability-10.0 roles at Google,
  Mastercard and Adobe. Added a `terminal_min_desirability` floor so the
  override also requires a job worth jumping the queue for
- 308 jobs held `fit_score = 0`, the scoring error sentinel, all from a single
  outage hour. Because `pending_score` keys off `fit_score IS NULL`, they were
  stranded permanently rather than retried; 297 were `new_grad`, dragging that
  cohort's mean fit from 5.23 to 3.91 and making internships look far stronger
  by comparison than they are. Reset to NULL so they re-enter the queue. (The
  guard that prevents writing the sentinel already exists; this is residue
  from before it landed.)
- 45 of 139 qualifying jobs had `is_terminal_internship` NULL -- one scoring
  batch wrote every other column but not that one. The flag is now derived by
  `compute_terminal_internships()` from stored columns, so it is repairable by
  re-running a free pass instead of a full LLM re-score
- Resume-variant swap on `grad_date_mismatch` picked an arbitrary other
  variant (`other_variants[0]`), which was correct only while exactly two
  existed. With six it could answer a returning-student posting with a
  May-2027 resume -- reintroducing the mismatch it exists to fix. It now flips
  only the graduation year and keeps the routed track
- `get_resume_variant_paths` falls back along a *year-preserving* chain when a
  variant's files aren't on disk, so a missing track resume degrades to the
  same-year SWE one rather than silently misstating the graduation date
- Scoring never saw the `salary` column, which is the only place pay actually
  lives (~40 of 121 jobs have a range there; 3 descriptions mention a figure).
  Every job therefore scored `PAY: not stated` / `BELOW_FLOOR: unknown` and
  the pay gate in `acquire_job` had never once fired
- Scoring now backfills `location` where discovery left it blank -- enrichment
  never wrote that column despite the NewGrad Jobs scraper's docstring saying
  it did, leaving half the board location-blind
- Account recovery (sign in -> reset via Gmail -> re-check dropped uploads)
- `--dry-run` no longer marks jobs applied, erases a prior real outcome, or
  re-acquires the same job
- Location check no longer rejects valid US postings
- Salary floor is intern-aware (hourly floor for internships, annual for
  full-time); salary field left blank when the form field itself is optional
- CDP watchdog ends a run when Chrome's DevTools port dies (was hanging ~10
  minutes)
- Resume-variant `start_date` was a single static profile value regardless
  of which resume was actually attached -- now variant-specific

### Changed
- Discovery freshness window widened 7 -> 14 days (safe to run on a
  recurring schedule; near-duplicates are deduped by the url primary key)
- Prompt efficiency: explicit `browser_type(slowly)` guidance for masked
  fields, batched `ToolSearch`/`browser_fill_form` calls, a guard against
  reading Playwright's own on-disk snapshot files via a shell tool

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
