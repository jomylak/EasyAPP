import type {
  ApplicationRow,
  AtsStat,
  DataStats,
  IpStats,
  CompanyLimitStatus,
  CostEstimate,
  DayBucket,
  EnvKeyName,
  EnvKeyStatus,
  Facets,
  FailureReasonStat,
  FingerprintRun,
  FingerprintTrend,
  GroupMember,
  JobDetail,
  JobsPage,
  QueueResponse,
  RunState,
  Settings,
  Stats,
} from "./types"

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => "")
    throw new Error(`${init?.method ?? "GET"} ${path} -> ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue
    p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ""
}

export interface JobsQuery {
  day?: string
  sort?: string
  dir?: string
  page?: number
  page_size?: number
  min_fit?: number | null
  min_desirability?: number | null
  min_prestige?: number | null
  min_pay?: number | null
  job_type?: string | null
  site?: string | null
  ats?: string | null
  q?: string
  above_pay_floor?: boolean
  terminal_only?: boolean
  likely_terminal_only?: boolean
  eligible_only?: boolean
  posted_within_days?: number | null
  // Narrow to big-tech postings.
  tier_only?: boolean
  // Exempt big-tech postings from the min_* bars instead of narrowing to
  // them -- how a prestige-10 posting with a fit of 3 still shows up in a
  // filtered view.
  include_tier?: boolean
  location?: string | null
  term?: string | null
  // Narrows to jobs reposted at most this many times (0 = never reposted).
  // Omitted/null = no filter, every job shows regardless of repost count.
  max_reposts?: number | null
}

export const api = {
  days: (filters: Record<string, string | number | boolean | null | undefined> = {}) =>
    request<{ days: DayBucket[] }>(`/api/days${qs(filters)}`),

  jobGroup: (url: string) => request<{ members: GroupMember[] }>(`/api/job/group${qs({ url })}`),

  // Browse-only, one-job-scoped hide (plus every future repost of it) --
  // distinct from reportIneligible below, which is Applications-tab-only and
  // sweeps every other posting at the same company.
  reportJobIneligible: (url: string) =>
    request<{ ok: boolean; reported_ineligible_at: string }>("/api/job/report-ineligible", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  jobs: (query: JobsQuery) =>
    request<JobsPage>(`/api/jobs${qs(query as Record<string, string | number | boolean | null | undefined>)}`),

  job: (url: string) => request<JobDetail>(`/api/job${qs({ url })}`),

  facets: () => request<Facets>("/api/facets"),

  estimateQueue: (urls: string[], backend?: string) =>
    request<CostEstimate>("/api/queue/estimate", {
      method: "POST",
      body: JSON.stringify({ urls, backend }),
    }),

  queue: (urls: string[], backend?: string) =>
    request<QueueResponse>("/api/queue", {
      method: "POST",
      body: JSON.stringify({ urls, backend }),
    }),

  reorderQueue: (urls: string[]) =>
    request<{ batch: string }>("/api/queue/reorder", {
      method: "POST",
      body: JSON.stringify({ urls }),
    }),

  unqueue: (urls: string[], revertStatus?: "failed") =>
    request<{ removed: number }>("/api/unqueue", {
      method: "POST",
      body: JSON.stringify({ urls, revert_status: revertStatus }),
    }),

  // Makes sure the always-on manual-queue worker pool is running -- a no-op
  // if it already is, since it drains every queued batch on its own. Fixed
  // at 8 workers server-side; there's only ever one such process.
  launch: (batch: string, opts: { dry_run?: boolean; backend?: string } = {}) =>
    request<{ batch: string; pid: number | null; jobs: number; log?: string; already_running?: boolean }>(
      "/api/launch",
      { method: "POST", body: JSON.stringify({ batch, ...opts }) },
    ),

  stats: () => request<{ stats: Stats; run: RunState | null; live: boolean }>("/api/stats"),

  applications: (status?: string | null, limit = 500) =>
    request<{ rows: ApplicationRow[] }>(`/api/applications${qs({ status, limit })}`),

  stop: (url: string) =>
    request<{ action: string; url: string }>("/api/stop", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  stopAll: () => request<{ stopped: boolean; released: number }>("/api/stop-all", { method: "POST" }),

  atsStats: () => request<{ rows: AtsStat[] }>("/api/ats-stats"),

  dataStats: () => request<DataStats>("/api/data-stats"),
  ipStats: () => request<IpStats>("/api/ip-stats"),

  companyLimits: () => request<{ rows: CompanyLimitStatus[] }>("/api/company-limits"),

  failureReasons: () => request<{ rows: FailureReasonStat[] }>("/api/failure-reasons"),

  fingerprintHistory: () =>
    request<{ rows: FingerprintRun[]; trend: FingerprintTrend | null }>("/api/fingerprint-history"),

  reportIneligible: (url: string, note?: string) =>
    request<{ job_marked: boolean; company: string | null; broad_employer: boolean | null; siblings_softened: number; siblings_disqualified: number }>(
      "/api/report-ineligible",
      { method: "POST", body: JSON.stringify({ url, note }) },
    ),

  setPostApplyStatus: (url: string, status: string) =>
    request<{ url: string; status: string }>("/api/post-apply-status", {
      method: "POST",
      body: JSON.stringify({ url, status }),
    }),

  settings: () => request<Settings>("/api/settings"),

  updateSettings: (patch: Partial<Settings>) =>
    request<Settings>("/api/settings", { method: "POST", body: JSON.stringify(patch) }),

  envKeys: () => request<EnvKeyStatus>("/api/env-keys"),

  setEnvKeys: (values: Partial<Record<EnvKeyName, string>>) =>
    request<{ updated: string[] }>("/api/env-keys", { method: "POST", body: JSON.stringify(values) }),

  openLoginSession: (url: string) =>
    request<{ ok: boolean }>("/api/login-session/open", { method: "POST", body: JSON.stringify({ url }) }),

  closeLoginSession: (reseedWorkers = false) =>
    request<{ ok: boolean; reseeded: number[] }>("/api/login-session/close", {
      method: "POST",
      body: JSON.stringify({ reseed_workers: reseedWorkers }),
    }),

  credentialCheck: () => request<{ alerts: string[] }>("/api/credential-check", { method: "POST" }),

  // Manual trigger for the same incremental Gmail sweep the continuous
  // pipeline runs on a timer -- for when a job applied to by hand should
  // show up sooner than the next loop tick.
  gmailScan: () =>
    request<{ emails: number; updated: number; new_manual: number; ambiguous: number }>(
      "/api/gmail-scan",
      { method: "POST" },
    ),
}
