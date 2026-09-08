import { useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { JobDetail, JobRow } from "@/lib/types"
import { formatPay, sanitizeDescription } from "@/lib/utils"

interface Props {
  row: JobRow
  open: boolean
}

/**
 * The expansion panel's content. Stays mounted regardless of `open` -- the
 * parent toggles a class on the wrapping <tr> so the grid-rows transition can
 * animate shut; unmounting this would just snap it closed instead.
 */
export function JobExpansion({ row, open }: Props) {
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
        if (!cancelled) setDetail(d)
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
          <dt>Pay floor</dt>
          <dd style={{ color: payFloorColor(payBelowFloor) }}>{payFloorLabel(payBelowFloor)}</dd>
          <dt>Applied</dt>
          <dd>{row.apply_status || "no"}</dd>
        </dl>
        {row.url && (
          <a
            href={row.url}
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
