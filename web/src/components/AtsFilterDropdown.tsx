import { useEffect, useRef, useState } from "react"

interface Props {
  options: string[]
  selected: string[]
  onChange: (next: string[]) => void
}

/**
 * A checklist behind one button, not a native multi-select -- a plain
 * <select multiple> needs ctrl/cmd-click to pick more than one, which reads
 * as broken to anyone who hasn't done it before. This is a single-choice
 * ATS filter's checklist replacement, so "Workday or Greenhouse, not Lever"
 * is one filter instead of needing to run the day list twice.
 */
export function AtsFilterDropdown({ options, selected, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", onDocClick)
    return () => document.removeEventListener("mousedown", onDocClick)
  }, [open])

  function toggle(name: string) {
    onChange(selected.includes(name) ? selected.filter((a) => a !== name) : [...selected, name])
  }

  const label =
    selected.length === 0 ? "Any ATS" : selected.length === 1 ? selected[0] : `${selected.length} ATS`

  return (
    <div className="ats-dropdown" ref={rootRef}>
      <button
        type="button"
        className={`select-field ats-dropdown-btn${selected.length ? " active" : ""}`}
        onClick={() => setOpen((o) => !o)}
      >
        {label} <span className="ats-caret">▾</span>
      </button>
      {open && (
        <div className="ats-dropdown-menu">
          {options.length === 0 && <div className="ats-dropdown-empty">No ATS data yet</div>}
          {options.map((a) => (
            <label className="ats-dropdown-item" key={a}>
              <input type="checkbox" checked={selected.includes(a)} onChange={() => toggle(a)} />
              {a}
            </label>
          ))}
          {selected.length > 0 && (
            <button type="button" className="ats-dropdown-clear" onClick={() => onChange([])}>
              Clear
            </button>
          )}
        </div>
      )}
    </div>
  )
}
