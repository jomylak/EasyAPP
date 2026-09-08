// Shapes returned by src/applypilot/web/server.py + queries.py. Kept close to
// the SQL column names on purpose -- these are read-only API responses, not a
// place to invent a parallel vocabulary.

export interface DayBucket {
  day: string
  total: number
  prestige: number
  best_fit: number
  applied: number
}

export interface JobRow {
  url: string
  title: string | null
  company: string | null
  site: string | null
  location: string | null
  salary: number | null
  pay_text: string | null
  // $/hr normalisation of `salary`/`pay_text` across all four stated period
  // formats (/hr, /wk, /mon, /yr) -- see src/applypilot/pay.py.
  pay_min_hourly: number | null
  pay_max_hourly: number | null
  // 'yes' | 'no' | 'unknown' | null -- judged from the description text at
  // scoring time, so this can be 'yes' even when `salary` is null. Most
  // postings state no pay at all, so unknown/null must render as unknown,
  // never as passing.
  pay_below_floor: "yes" | "no" | "unknown" | null
  fit_score: number | null
  desirability_score: number | null
  company_prestige: number | null
  // 'tier1' | 'adjacent' | null -- see config.TIER1_COMPANIES. Pinned above
  // the ranking rather than weighted into it.
  company_tier: string | null
  job_type: string | null
  ats: string | null
  eligible: string | null
  keywords: string | null
  // spring | summer | fall | winter | rolling | unclear | null -- the LLM's
  // own read of the posting's academic term (TERM CHECK in SCORE_PROMPT).
  term: string | null
  // 'yes' | 'no' | null -- explicit evidence the posting welcomes an
  // already-graduated candidate, cleared the fit/desirability bar. Absolute
  // top apply-queue priority, same tier as is_remote_spring_internship.
  is_terminal_internship: string | null
  // 'yes' | 'no' | null -- strong match, posting says nothing either way
  // about post-grad eligibility. Worth a manual look, not queue-boosted.
  is_terminal_internship_likely: string | null
  // 'yes' | 'no' | null -- fully-remote Spring-term internship; needs no
  // grad-date evidence at all since the term ends before graduation. Same
  // top apply-queue priority tier as is_terminal_internship.
  is_remote_spring_internship: string | null
  apply_status: string | null
  applied_at: string | null
  apply_error: string | null
  apply_cost_usd: number | null
  queue_batch: string | null
  tailored_resume_path: string | null
  day: string | null
  posted: string | null
}

export interface JobDetail extends JobRow {
  full_description: string | null
  description: string | null
  application_url: string | null
  score_reasoning: string | null
  reasoning: string | null
  resume_variant: string | null
  review_status: string | null
  apply_attempts: number | null
  is_terminal_internship: string | null
  requires_returning_student: string | null
  eligibility_reason: string | null
  resume_route: {
    track?: string
    matched_on?: string[]
    candidates?: string[]
  } | null
}

export interface JobsPage {
  rows: JobRow[]
  total: number
  page: number
  page_size: number
  sort: string
}

export interface Facets {
  sites: string[]
  job_types: string[]
  ats: string[]
  sorts: string[]
}

export interface CostEstimate {
  expected: number
  low: number
  high: number
  n_jobs: number
  n_samples: number
  basis: string
}

export interface QueueResponse {
  batch: string
  queued: number
  skipped: number
  estimate: CostEstimate
  backend: string
}

export type SortKey =
  // Big tech pinned first, then desirability, with fit as a pure tiebreaker.
  // The default -- fit used to lead and it buried the best postings.
  | "top"
  | "prestige"
  | "fit"
  | "desirability"
  | "company"
  | "title"
  | "posted"
  | "pay"

// Filters a single DayTable owns, scoped to its own day. Mirrors the query
// params /api/jobs accepts, minus `day`/`page`/`page_size`/`sort`, which the
// table already tracks separately.
export interface DayFilters {
  min_fit: number | null
  min_desirability: number | null
  min_prestige: number | null
  min_pay: number | null
  q: string
}

export const EMPTY_DAY_FILTERS: DayFilters = {
  min_fit: null,
  min_desirability: null,
  min_prestige: null,
  min_pay: null,
  q: "",
}

// The "what am I willing to look at at all" layer, narrowing every day.
export interface GlobalFilters {
  site: string | null
  job_type: string | null
  // Multiple ATSes checked at once (empty = any), not a single choice --
  // e.g. "Workday or Greenhouse, not Lever" in one filter.
  ats: string[]
  above_pay_floor: boolean
  unapplied_only: boolean
  // Confirmed terminal: is_terminal_internship = 'yes' -- posting explicitly
  // welcomes an already-graduated candidate.
  terminal_only: boolean
  // Manual-review queue: is_terminal_internship_likely = 'yes' only -- a
  // strong match whose posting never addresses post-grad eligibility either
  // way, so it's worth a human look rather than an automatic queue boost.
  likely_terminal_only: boolean
  // Big tech only -- the "closed mouths don't get fed" view. Every FAANG /
  // big-tech posting gets applied to regardless of how its fit scores.
  tier_only: boolean
  // Coarse location bucket: "nyc" | "metro" | "remote" | null. Matches
  // scorer._location_desirability's own tiers so the filter and the score
  // can't disagree about what counts as NYC.
  location: string | null
  // Internship season: "spring" | "summer" | null.
  term: string | null
  q: string
}

// ---------------------------------------------------------------------------
// Dashboard tab -- src/applypilot/web/queries.py:stats/applications,
// src/applypilot/apply/dashboard.py's run_state.json mirror.
// ---------------------------------------------------------------------------

export interface Stats {
  total: number
  applied: number
  failed: number
  queued: number
  in_progress: number
  manual: number
  scored: number
  pending_enrich: number
  needs_review: number
  spend: number
  priced_attempts: number
}

export interface ApplicationRow extends JobRow {
  apply_attempts: number | null
  apply_backend: string | null
  review_status: string | null
  last_attempted_at: string | null
  apply_duration_ms: number | null
  resume_variant: string | null
}

// Mirrors dashboard.py's WorkerState dataclass, plus the `url` field it
// carries specifically for the web UI to join a worker back to its row.
export interface WorkerState {
  worker_id: number
  status: string
  job_title: string
  company: string
  // 'tier1' | 'adjacent' | null -- same field as JobRow.company_tier, so the
  // live worker row can get the same gold treatment as a table row.
  company_tier: string | null
  score: number
  start_time: number
  actions: number
  last_action: string
  jobs_applied: number
  jobs_failed: number
  jobs_done: number
  total_cost: number
  log_file: string | null
  url: string
}

export interface RunState {
  pid: number
  batch: string | null
  backend: string
  dry_run: boolean
  started_at: string
  finished_at?: string
  updated_at: string
  workers: WorkerState[]
  events: string[]
  totals: { applied: number; failed: number; cost: number }
}

export interface RunEvent {
  run: RunState | null
  live: boolean
}

// src/applypilot/costs.py:ats_stats() -- per (ATS, backend) run history.
export interface AtsStat {
  ats: string
  backend: string
  n_runs: number
  n_applied: number
  success_rate: number
  median_cost_usd: number | null
  total_cost_usd: number
  median_duration_s: number | null
}

// src/applypilot/scoring/scorer.py:compute_desirability -- the three
// independent components of a desirability score, weighted per lane. Pay and
// location used to be one folded term, which silently discarded pay whenever
// the location scored well on its own.
export interface LaneWeights {
  pay: number
  prestige: number
  location: number
}

// src/applypilot/config.py:DEFAULT_SETTINGS -- the full settings.json shape.
// Every numeric cap here is nullable: null means "use the built-in default"
// (config.DEFAULTS) for the per-job knobs, or "no cap" for the daily ones.
export interface Settings {
  apply_backend: "goose" | "claude"
  apply_fallback_backend: string | null
  cost_defaults: Record<string, number>
  max_daily_spend_usd: number | null
  max_daily_applications: number | null
  max_apply_attempts: number | null
  apply_timeout: number | null
  goose_timeout: number | null
  goose_max_turns: number | null
  goose_max_tool_repetitions: number | null
  goose_writes_quirks: boolean
  tailoring_enabled: boolean
  cover_letters_enabled: boolean
  graduation_date: string
  earliest_start_date: string
  new_grad_weights: LaneWeights
  internship_weights: LaneWeights
  [key: string]: unknown
}

export const KNOWN_ENV_KEYS = [
  "GEMINI_API_KEY",
  "OPENAI_API_KEY",
  "OPENROUTER_API_KEY",
  "LLM_URL",
  "APPLYPILOT_JOB_PASSWORD",
] as const
export type EnvKeyName = (typeof KNOWN_ENV_KEYS)[number]
export type EnvKeyStatus = Record<EnvKeyName, boolean>

export const EMPTY_GLOBAL_FILTERS: GlobalFilters = {
  site: null,
  job_type: null,
  ats: [],
  above_pay_floor: false,
  unapplied_only: false,
  terminal_only: false,
  likely_terminal_only: false,
  tier_only: false,
  location: null,
  term: null,
  q: "",
}
