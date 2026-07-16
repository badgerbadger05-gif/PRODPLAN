// Types for the DBR (drum-buffer-rope) parallel planning module.
// Mirrors backend/app/routers/dbr.py Pydantic schemas. Decimal fields are
// serialized as strings/numbers by FastAPI, so we accept `number | string`.

export type DbrSettings = {
  id: number
  frozen_days: number
  gate_horizon_workdays: number
  shelf_threshold_qty: number | string
  rt_machining_days: number
  rt_welding_days: number
  rt_painting_days: number
  batch_days_turning: number
  batch_days_bending: number
  batch_days_welding: number
  batch_days_paint_black: number
  batch_days_paint_color: number
  feeder_chain_enabled: boolean
  feeder_load_horizon_weeks: number
  w2_warehouse_ref1c?: string | null
  w3_warehouse_ref1c?: string | null
  w4_warehouse_ref1c?: string | null
}

// All fields optional — the backend patches only what is sent (exclude_unset).
export type DbrSettingsUpdate = Partial<Omit<DbrSettings, 'id'>>

export type DbrAssemblyRate = {
  id: number
  resource_id: number
  resource_name: string
  item_id: number
  item_code: string
  item_name: string
  qty_per_capacity: number | string
}

export type DbrAssemblyRateUpsert = {
  resource_id: number
  item_id: number
  qty_per_capacity: number | string
}

export type DbrCategoryRisk = {
  id: number
  item_group: string
  receipt_warehouse_ref1c?: string | null
  supply_risk_pct?: number | string | null
}

export type DbrCategoryRiskIn = {
  item_group: string
  receipt_warehouse_ref1c?: string | null
  supply_risk_pct?: number | string | null
}
