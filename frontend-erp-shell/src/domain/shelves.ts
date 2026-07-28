import type { TruthMeta } from './truth'

// One drum demand this shelf line defends. `qty` arrives as a decimal string
// (backend keeps the exact Decimal), so it is formatted, never summed here.
export type ShelfDemandManifestEntry = {
  need_date: string
  qty: string
  priority: Array<string | number>
  planning_run_id: number
  plan_id: number
  plan_line_id: number
  drum_slot_id: number
  freeze_component_id: number
}

export type ShelfProjectionRow = {
  policy_id: number
  item_id: number
  item_code: string | null
  item_name: string | null
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
  demand_manifest: ShelfDemandManifestEntry[]
}

export type ShelvesResponse = {
  rows: ShelfProjectionRow[]
  total_rows: number
  limit: number
  offset: number
  truth_meta: TruthMeta
}
