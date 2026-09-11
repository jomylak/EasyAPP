import { useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { JobDetail, JobRow } from "@/lib/types"
import { formatDaysAgo, formatPay, sanitizeDescription } from "@/lib/utils"

interface Props {
  row: JobRow
  open: boolean
  // Patches the list row with any fields the detail fetch came back richer
  // on (pay/location in particular) -- the list is fetched once per filter
  // change and the background enrichment scraper can fill those in after
  // that fetch, so without this the list keeps showing "—" until the next
  // refetch even though this same job's expansion already has the answer.
  onDetailLoaded?: (url: string, detail: JobDetail) => void
}

/**
 * The expansion panel's content. Stays mounted regardless of `open` -- the
 * parent toggles a class on the wrapping <tr> so the grid-rows transition can
 * animate shut; unmounting this would just snap it closed instead.
 */
export function JobExpansion({ row, open, onDetailLoaded }: Props) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  // A ref, not state: putting a "have we started fetching" flag in the
  // effect's own dependency array made it re-run right after being set,
  // whose cleanup then marked the in-flight fetch `cancelled` before it ever
  // resolved -- the panel fetched successfully every time and discarded the
  // answer, leaving "Loading..." on screen forever.
  const startedRef = useRef(false)

  useEffect(() => {
    if (!open || startedRef.current) return
    startedRef.current = true
    let cancelled = false
    api
      .job(row.url)
      .then((d) => {
        if (!cancelled) {
          setDetail(d)
          onDetailLoaded?.(row.url, d)
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
  }, [open, row.url])

  const description = detail
    ? sanitizeDescription(detail.full_description || detail.description)
    : ""
  const keywords = (detail?.keywords || row.keywords || "")
    .split(/[,\n]/)
    .map((k) => k.trim())
    .filter(Boolean)

  // Judged from the description text at scoring time, independent of
  // whether `salary` parsed -- a row can be 'yes' with a null salary. Most
  // postings state no pay at all, so unknown/null must read as unknown,
  // never as passing. Prefer the fetched detail once it lands; the list row
  // already carries the same field so there is no flash of "unknown" first.
  const payBelowFloor = detail?.pay_below_floor ?? row.pay_below_floor ?? null

  return (
    <div className="expbox">
      <div>
        <h4>Job description</h4>
        {!detail && !error && <p style={{ color: "var(--a-text-3)" }}>Loading…</p>}
        {error && <p style={{ color: "var(--a-bad)" }}>Could not load: {error}</p>}
        {detail && (
          <div className="jd">{description || "No description on file for this posting."}</div>
        )}

        {detail?.reasoning && (
          <>
            <h4 style={{ marginTop: 16 }}>Why this score</h4>
            <p className="reason">{detail.reasoning}</p>
          </>
        )}

        {keywords.length > 0 && (
          <>
            <h4 style={{ marginTop: 16 }}>Keywords found</h4>
            <div className="kw">
              {keywords.map((k) => (
                <span className="tag" key={k}>
                  {k}
                </span>
              ))}
            </div>
          </>
        )}
      </div>

      <div>
        <dl className="kv">
          <dt>Company</dt>
          <dd>
            {row.company ? (
              <span className={row.company_tier === "tier1" ? "tier1-company" : row.company_tier ? "tier-adjacent" : undefined}>
                {row.company}
              </span>
            ) : (
              "—"
            )}
          </dd>
          <dt>Title</dt>
          <dd>{row.title || "—"}</dd>
          <dt>Posted</dt>
          <dd title={row.posted ? new Date(row.posted).toLocaleString() : undefined}>
            {formatDaysAgo(row.posted)}
          </dd>
          <dt>Pay</dt>
          <dd>{formatPay(row)}</dd>
          <dt>Location</dt>
          <dd>{row.location || "—"}</dd>
          <dt>Type</dt>
          <dd>{row.job_type || "—"}</dd>
          <dt>ATS</dt>
          <dd>{row.ats || "—"}</dd>
          {/* One resume now, so this reports the routed *track* (swe / aiml /
              data) as a label on the posting, not a file that gets swapped
              in. resume_variant is legacy data on rows tailored before the
              variant system was removed. */}
          <dt>Track</dt>
          <dd>{detail?.resume_route?.track || detail?.resume_variant || "—"}</dd>
          <dt>Eligible</dt>
          <dd style={{ color: row.eligible === "no" ? "var(--a-bad)" : "var(--a-good)" }}>
            {row.eligible === "no" ? "no" : row.eligible || "yes"}
          </dd>
          {detail?.eligibility_reason && (
            <>
              <dt>Eligibility reason</dt>
              <dd>{detail.eligibility_reason}</dd>
            </>
          )}
          {detail?.requires_returning_student && (
            <>
              <dt>Requires returning student</dt>
              <dd>{detail.requires_returning_student}</dd>
            </>
          )}
          <dt>Pay floor</dt>
          <dd style={{ color: payFloorColor(payBelowFloor) }}>{payFloorLabel(payBelowFloor)}</dd>
          <dt>Applied</dt>
          <dd>{row.apply_status || "no"}</dd>
          {detail && (detail.apply_attempts ?? 0) > 0 && (
            <>
              <dt>Apply attempts</dt>
              <dd>{detail.apply_attempts}</dd>
            </>
          )}
          {detail?.apply_error && (
            <>
              <dt>Failure reason</dt>
              <dd style={{ color: "var(--a-bad)" }}>{detail.apply_error}</dd>
            </>
          )}
          {detail?.review_status && (
            <>
              <dt>Review status</dt>
              <dd>{detail.review_status}</dd>
            </>
          )}
          {detail?.company_limit && (
            <>
              <dt>Company cap</dt>
              <dd style={{ color: detail.company_limit.at_cap ? "var(--a-bad)" : "var(--a-text)" }}>
                {detail.company_limit.applied}/{detail.company_limit.limit} {periodLabel(detail.company_limit.period)}
                {detail.company_limit.at_cap ? " · at cap" : ""}
              </dd>
            </>
          )}
          {detail?.last_attempted_at && (
            <>
              <dt>Last attempted</dt>
              <dd>{new Date(detail.last_attempted_at).toLocaleString()}</dd>
            </>
          )}
          {detail?.apply_backend && (
            <>
              <dt>Apply backend</dt>
              <dd>
                {detail.apply_backend}
                {detail.apply_duration_ms ? ` · ${formatDuration(detail.apply_duration_ms)}` : ""}
              </dd>
            </>
          )}
          {detail?.apply_cost_usd != null && (
            <>
              <dt>Apply cost</dt>
              <dd>
                ${detail.apply_cost_usd.toFixed(2)}
                {detail.apply_llm_requests ? ` · ${detail.apply_llm_requests} LLM calls` : ""}
              </dd>
            </>
          )}
          {(detail?.apply_input_tokens != null || detail?.apply_output_tokens != null) && (
            <>
              <dt>Tokens</dt>
              <dd>
                {detail?.apply_input_tokens ?? 0} in · {detail?.apply_output_tokens ?? 0} out
                {detail?.apply_cache_read_tokens ? ` · ${detail.apply_cache_read_tokens} cached` : ""}
              </dd>
            </>
          )}
        </dl>
        {(detail?.application_url || row.url) && (
          <a
            href={detail?.application_url || row.url}
            target="_blank"
            rel="noreferrer"
            style={{
              display: "inline-block",
              marginTop: 14,
              fontSize: 12,
              color: "var(--a-sel)",
            }}
          >
            Open original posting →
          </a>
        )}
      </div>
    </div>
  )
}

function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

function periodLabel(period: "total" | "month" | "season"): string {
  if (period === "total") return "overall"
  return `this ${period}`
}

function payFloorLabel(v: JobRow["pay_below_floor"]): string {
  if (v === "yes") return "below floor"
  if (v === "no") return "clears"
  return "unknown"
}

function payFloorColor(v: JobRow["pay_below_floor"]): string {
  if (v === "yes") return "var(--a-bad)"
  if (v === "no") return "var(--a-good)"
  return "var(--a-text-3)"
}
