export type DrumPriorityPart = string | number

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

export type DrumTruthMeta = {
  ledger_generation: number
  cutoff: string
  truth_status: string
  truth_reason: string | null
}

export type DrumResponse = {
  schedule_from: string
  schedule_to: string
  slots: DrumSlotRow[]
  gaps: DrumCapacityGapRow[]
  total_open_qty: number
  total_slot_qty: number
  total_gap_qty: number
  truth_meta: DrumTruthMeta
}
