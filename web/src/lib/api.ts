import type {
  ApplicationRow,
  AtsStat,
  CompanyLimitStatus,
  CostEstimate,
  DayBucket,
  EnvKeyName,
  EnvKeyStatus,
  Facets,
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
  unapplied_only?: boolean
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
}

export const api = {
  days: () => request<{ days: DayBucket[] }>("/api/days"),

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

  launch: (batch: string, opts: { workers?: number; dry_run?: boolean; backend?: string } = {}) =>
    request<{ batch: string; pid: number; jobs: number; log: string }>("/api/launch", {
      method: "POST",
      body: JSON.stringify({ batch, ...opts }),
    }),

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

  companyLimits: () => request<{ rows: CompanyLimitStatus[] }>("/api/company-limits"),

  settings: () => request<Settings>("/api/settings"),

  updateSettings: (patch: Partial<Settings>) =>
    request<Settings>("/api/settings", { method: "POST", body: JSON.stringify(patch) }),

  envKeys: () => request<EnvKeyStatus>("/api/env-keys"),

  setEnvKeys: (values: Partial<Record<EnvKeyName, string>>) =>
    request<{ updated: string[] }>("/api/env-keys", { method: "POST", body: JSON.stringify(values) }),
}
