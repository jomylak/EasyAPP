interface Props {
  selectedTypes: Map<string, string | null>
}

// The target share of a week's applications that should go to new-grad
// roles. The other 40% is internships: a Spring or Summer 2027 internship
// either converts or buys experience, so it's worth real volume -- just not
// the majority of it.
const TARGET_NEW_GRAD_PCT = 60
// How far the actual share can drift before the counter says so. Under ten
// points is rounding on a batch of twenty; past it, the week has genuinely
// skewed to one lane.
const DRIFT_TOLERANCE_PCT = 10

/**
 * Live 60/40 readout over everything currently ticked, across both Priority
 * lanes and every day table.
 *
 * This is the whole enforcement mechanism for the split, and it deliberately
 * only reports: it never blocks a launch, never rebalances a selection, and
 * never greys out a checkbox. A hard quota would be wrong here -- some weeks
 * genuinely are all internships because that's what got posted -- so the job
 * is to make the drift visible while there's still time to fix it, not to
 * decide on the user's behalf.
 */
export function SplitCounter({ selectedTypes }: Props) {
  let newGrad = 0
  let internship = 0
  for (const t of selectedTypes.values()) {
    if (t === "new_grad") newGrad += 1
    else if (t === "internship") internship += 1
  }
  const total = newGrad + internship
  if (total === 0) {
    return <div className="split-counter">nothing selected · target 60/40</div>
  }

  const pct = Math.round((newGrad / total) * 100)
  const off = Math.abs(pct - TARGET_NEW_GRAD_PCT) > DRIFT_TOLERANCE_PCT

  return (
    <div className={`split-counter${off ? " off" : ""}`}>
      <span>
        New grad {newGrad} · Internship {internship}
      </span>
      <span className="split-bar">
        <i style={{ width: `${pct}%` }} />
        <span className="split-target" style={{ left: `${TARGET_NEW_GRAD_PCT}%` }} />
      </span>
      <span className="split-pct">
        {pct}/{100 - pct}
        {off ? " ⚠" : ""}
      </span>
    </div>
  )
}
