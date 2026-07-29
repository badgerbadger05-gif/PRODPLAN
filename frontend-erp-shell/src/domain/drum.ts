import type { TruthMeta } from './truth'

export type DrumPriorityPart = string | number

// `item_code`/`item_name` — необязательные: старая persisted-версия барабана
// (`app.routers.production_control.DrumSlotRow`) их не несёт, строка тогда
// опознаётся только по `item_id`.
export type DrumItemIdentity = {
  item_id: number
  item_code?: string | null
  item_name?: string | null
}

export type DrumSlotRow = DrumItemIdentity & {
  plan_id: number
  plan_line_id: number
  resource_id: number
  slot_date: string
  slot_qty: number
  slot_ordinal: number
  original_priority: DrumPriorityPart[]
}

export type DrumCapacityGapRow = DrumItemIdentity & {
  plan_id: number
  plan_line_id: number
  resource_id: number
  gap_date: string
  gap_qty: number
  original_priority: DrumPriorityPart[]
}

// «код — имя», пока backend их отдаёт; иначе остаётся сырой item_id.
export function drumItemLabel(row: DrumItemIdentity) {
  const code = (row.item_code ?? '').trim()
  const name = (row.item_name ?? '').trim()
  if (code && name) return `${code} — ${name}`
  return code || name || String(row.item_id)
}

export type DrumResponse = {
  schedule_from: string
  schedule_to: string
  slots: DrumSlotRow[]
  gaps: DrumCapacityGapRow[]
  total_open_qty: number
  total_slot_qty: number
  total_gap_qty: number
  // Whole-schedule counts; `slots`/`gaps` are the requested window of each.
  total_slots: number
  total_gaps: number
  limit: number
  offset: number
  truth_meta: TruthMeta
}
