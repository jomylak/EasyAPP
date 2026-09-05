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
  job_type: string | null
  ats: string | null
  eligible: string | null
  keywords: string | null
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
  ats: string | null
  eligible_only: boolean
  above_pay_floor: boolean
  unapplied_only: boolean
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

export const EMPTY_GLOBAL_FILTERS: GlobalFilters = {
  site: null,
  job_type: null,
  ats: null,
  eligible_only: false,
  above_pay_floor: false,
  unapplied_only: false,
  q: "",
}
