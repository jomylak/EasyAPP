import { cn } from "@/lib/utils"

interface Props {
  id: string
  label: string
  value: number | null
  onChange: (v: number | null) => void
  step?: number
  unit?: string
}

/** A typed threshold, not a fixed bucket -- any number the user types. */
export function NumericFilter({ id, label, value, onChange, step = 1, unit }: Props) {
  const active = value !== null && value > 0
  return (
    <span className={cn("numf", active && "active")}>
      <label htmlFor={id}>{label} &ge;</label>
      <input
        id={id}
        type="number"
        min={0}
        step={step}
        placeholder="0"
        // "" rather than 0 when unset -- a real 0 left in the box reads as a
        // stray leading digit the moment someone starts typing over it.
        value={value ?? ""}
        onFocus={(e) => e.target.select()}
        onChange={(e) => {
          const n = parseFloat(e.target.value)
          onChange(Number.isFinite(n) && n > 0 ? n : null)
        }}
      />
      {unit && <span className="unit">{unit}</span>}
    </span>
  )
}
