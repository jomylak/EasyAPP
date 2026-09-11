import type { JobRow } from "@/lib/types"

// Presentational grouping only -- this never hides a row, so it can afford
// to be coarser than the backend's auto-dedup match (dedup.py's
// find_company_duplicate, which requires an exact location match and a
// strict description-similarity bar before it sets duplicate_of). A
// false-positive grouping here just means an expand list with one row that
// doesn't quite belong -- not a lost application -- so the bar for
// clustering two rows together is deliberately lower than the bar for
// auto-hiding one.
//
// The point is the TikTok/ByteDance case: a company that genuinely posts a
// large number of distinct-but-similarly-titled roles (different teams,
// different locations) floods Browse with rows a human has to scroll past
// one at a time. Collapsing them into one expandable summary row keeps
// every row reachable -- nothing is decided for the user -- while making
// the common case (skim past, or drill into exactly this company) cheap.

const LEGAL_SUFFIXES =
  /\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|technologies|technology|labs|platforms|securities|systems)\b/g

function normalizeCompany(name: string | null): string {
  let s = (name ?? "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ")
  s = s.replace(LEGAL_SUFFIXES, " ")
  return s.replace(/\s+/g, " ").trim()
}

// Strips a trailing "- City, ST" / "(Remote)" so genuinely-different-office
// reqs with the same role name still cluster together for display, even
// though the backend's auto-dedup deliberately requires an exact location
// match and would NOT merge them (see dedup.py's find_company_duplicate
// docstring: a different office is a different req, not a repost).
function normalizeTitleForClustering(title: string | null): string {
  let s = (title ?? "").toLowerCase()
  s = s.replace(/\s*[-–—]\s*[a-z .]+,\s*[a-z]{2}\s*$/i, "")
  s = s.replace(/\(remote\)|\bremote\b/gi, "")
  s = s.replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim()
  return s
}

// Below this, showing rows individually reads clearer than collapsing them
// into a 2-row cluster -- tune by eye against real Browse data, there's no
// correctness stake here since nothing is ever hidden.
const MIN_CLUSTER_SIZE = 3

export interface JobCluster {
  kind: "cluster"
  key: string
  company: string | null
  titleLabel: string
  rows: JobRow[]
  // Mean over the group's non-null values, so one row missing a score
  // doesn't read as a 0 dragging the average down -- null when every row
  // in the group is null for that field.
  avgPrestige: number | null
  avgFit: number | null
  avgDesirability: number | null
  // Most recent posted date across the group -- matches the default
  // newest-first sort everywhere else, and answers "is this company still
  // actively posting this role right now" rather than "when did this
  // first show up."
  latestPosted: string | null
}

export type ClusterEntry = JobRow | JobCluster

export function isCluster(entry: ClusterEntry): entry is JobCluster {
  return (entry as JobCluster).kind === "cluster"
}

function average(values: (number | null)[]): number | null {
  const present = values.filter((v): v is number => v !== null && v !== undefined)
  if (!present.length) return null
  return present.reduce((a, b) => a + b, 0) / present.length
}

function latestOf(dates: (string | null)[]): string | null {
  const present = dates.filter((d): d is string => !!d)
  if (!present.length) return null
  return present.reduce((a, b) => (b > a ? b : a))
}

function clusterKey(r: JobRow): string | null {
  const company = normalizeCompany(r.company)
  const title = normalizeTitleForClustering(r.title)
  if (!company || !title) return null
  return `${company}::${title}`
}

// Groups `rows` by (normalized company, normalized title), keeping every
// group under MIN_CLUSTER_SIZE as plain individual rows and collapsing the
// rest into one JobCluster placeholder, emitted at the position of the
// group's first member so the result preserves the table's active sort
// order (a cluster surfaces where its best/newest member would have).
export function clusterRows(rows: JobRow[]): ClusterEntry[] {
  const groups = new Map<string, JobRow[]>()
  for (const r of rows) {
    const key = clusterKey(r)
    if (!key) continue
    const g = groups.get(key)
    if (g) g.push(r)
    else groups.set(key, [r])
  }

  const emitted = new Set<string>()
  const out: ClusterEntry[] = []
  for (const r of rows) {
    const key = clusterKey(r)
    const group = key ? groups.get(key) : undefined
    if (group && group.length >= MIN_CLUSTER_SIZE) {
      if (emitted.has(key!)) continue
      emitted.add(key!)
      out.push({
        kind: "cluster",
        key: key!,
        company: r.company,
        titleLabel: r.title ?? "",
        rows: group,
        avgPrestige: average(group.map((g) => g.company_prestige)),
        avgFit: average(group.map((g) => g.fit_score)),
        avgDesirability: average(group.map((g) => g.desirability_score)),
        latestPosted: latestOf(group.map((g) => g.posted)),
      })
    } else {
      out.push(r)
    }
  }
  return out
}
