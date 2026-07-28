import type { TruthMeta } from './truth'

export type DrumPriorityPart = string | number

// The persisted drum carries no item names (see
// `app.routers.production_control.DrumSlotRow`): slots reference `item_id` and
// `resource_id` only.
export type DrumSlotRow = {
  plan_id: number
  plan_line_id: number
  item_id: number
  resource_id: number
  slot_date: string
  slot_qty: number
  slot_ordinal: number
  original_priority: DrumPriorityPart[]
}

export type DrumCapacityGapRow = {
  plan_id: number
  plan_line_id: number
  item_id: number
  resource_id: number
  gap_date: string
  gap_qty: number
  original_priority: DrumPriorityPart[]
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
