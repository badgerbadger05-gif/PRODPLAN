export type ShelfProjectionRow = {
  policy_id: number
  item_id: number
  warehouse_ref1c: string
  protection_until: string
  target_qty: number
  shelf_physical_qty: number
  other_stock_qty: number
  projected_qty: number
  gap_qty: number
  transfer_qty: number
  unlaunched_mrp_qty: number
  pull_qty: number
  materialized_qty: number
  first_shortage_date: string | null
  latest_start_date: string | null
}

export type ShelvesTruthMeta = {
  ledger_generation: number
  cutoff: string
  truth_status: string
  truth_reason: string | null
}

export type ShelvesResponse = {
  rows: ShelfProjectionRow[]
  total_rows: number
  truth_meta: ShelvesTruthMeta
}
