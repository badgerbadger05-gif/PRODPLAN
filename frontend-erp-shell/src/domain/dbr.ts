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
  fastener_categories: string[]
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

// ── Production program (производственная программа) ───────────────────────────

export type DbrProgramStatus = 'draft' | 'approved' | string

export type DbrProgramItem = {
  id: number
  item_id: number
  item_code?: string | null
  item_name?: string | null
  program_date: string
  qty: number
  comment?: string | null
}

export type DbrProgram = {
  id: number
  company?: string | null
  title?: string | null
  from_date: string
  to_date: string
  status: DbrProgramStatus
  created_by?: string | null
  items: DbrProgramItem[]
}

export type DbrProgramItemIn = {
  item_id: number
  program_date: string
  qty: number | string
  comment?: string | null
}

export type DbrProgramCreate = {
  from_date: string
  to_date: string
  company?: string | null
  title?: string | null
  created_by?: string | null
  items: DbrProgramItemIn[]
}

export type DbrProgramUpdate = {
  from_date?: string
  to_date?: string
  company?: string | null
  title?: string | null
  items?: DbrProgramItemIn[]
}

// ── Drum schedule + board ─────────────────────────────────────────────────────

export type DbrScheduleStatus = 'draft' | 'active' | 'superseded' | 'cancelled' | string

export type DbrSchedule = {
  id: number
  period_from: string
  period_to: string
  source_program_id?: number | null
  status: DbrScheduleStatus
  config_snapshot?: unknown
}

export type DbrKitStatus = 'green' | 'yellow' | 'red' | 'unknown' | string

export type DbrShortage = {
  item: string
  required: number
  available: number
  warehouse?: string | null
}

export type DbrBoardSlot = {
  id: number
  date: string
  planned_date?: string | null
  resource_id: number
  resource_name?: string | null
  item_id: number
  item_code?: string | null
  item_name?: string | null
  qty: number
  produced_qty: number
  kit_status: DbrKitStatus
  release_status?: string | null
  shortage?: DbrShortage[] | null
  position: number
}

export type DbrBoardGap = {
  id: number
  date?: string | null
  resource_id?: number | null
  resource_name?: string | null
  item_id?: number | null
  item_code?: string | null
  item_name?: string | null
  required_qty: number
  takt_qty: number
  gap_qty: number
  resolution?: string | null
}

export type DbrBoardResource = { id: number; name?: string | null }

export type DbrBoardKpi = {
  green: number
  yellow: number
  red: number
  unknown: number
  slots: number
  plan_qty: number
  fact_qty: number
}

export type DbrBoard = {
  schedule: DbrSchedule | null
  days: string[]
  resources: DbrBoardResource[]
  slots: DbrBoardSlot[]
  gaps: DbrBoardGap[]
  kpi: DbrBoardKpi
  calendar_fallback?: boolean
}

export type DbrBuildResult = {
  schedule: DbrSchedule
  slots_added?: number
  carried_over?: unknown[]
  calendar_fallback?: boolean
}

export type DbrGateResult = {
  updated: number
  green: number
  yellow: number
  red: number
  notes?: string[]
}

export type DbrRollForwardResult = {
  moved: number
  closed: number
  overloaded: number
  no_schedule?: boolean
  horizon_exhausted?: boolean
  no_workdays?: boolean
}

export type DbrMoveResult = {
  ok: boolean
  moved: boolean
  from?: string
  to?: string
}

export type DbrReleaseResult = {
  ok: boolean
  slot_id: number
  release_status: string
  already_released: boolean
  stub: boolean
  note?: string
}

// ── Feeder-chain supermarket positions ──────────────────────────────────────

export type DbrFeederZone = 'green' | 'yellow' | 'red' | string

export type DbrFeederLiveNfp = {
  stock_qty: number
  open_supply_qty: number
  qualified_demand_qty: number
  nfp: number
  zone: DbrFeederZone
  penetration: number
  is_complete: boolean
  missing_reasons: string[]
  data_quality: string[]
  formula: string
  timestamps: {
    stock_as_of?: string | null
    supply_as_of?: string | null
    position_calculated_at?: string | null
    live_calculated_at?: string | null
  }
}

export type DbrFeederPosition = {
  id: number
  item_id: number
  item_code: string
  item_name: string
  warehouse_ref1c: string
  supply_type: 'purchase' | 'manufacture' | string
  mode: 'shelf' | 'under_schedule' | string
  adu: number | string
  commonality: number
  route_class?: string | null
  red_qty: number | string
  yellow_qty: number | string
  green_qty: number | string
  target_qty: number | string
  data_quality: string[]
  source_schedule_id: number
  is_active: boolean
  is_stale: boolean
  calculated_at?: string | null
  live_nfp?: DbrFeederLiveNfp
}

export type DbrFeederPreview = {
  schedule_id: number
  positions: DbrFeederPosition[]
  warnings: string[]
  created?: number
  updated?: number
  deactivated?: number
}

export type DbrFeederFilters = {
  active_only?: boolean
  mode?: string
  supply?: string
  zone?: string
  search?: string
  limit?: number
  offset?: number
}

// Advisory only: these records describe replenishment demand but never launch
// production, create purchase orders, or write to 1C.
export type DbrFeederSignalStatus = 'Open' | 'Cancelled' | string

export type DbrFeederSignal = {
  id: number
  dedup_key: string
  signal_type: string
  position_id: number
  item_id: number
  item_code?: string | null
  item_name?: string | null
  warehouse_ref1c: string
  status: DbrFeederSignalStatus
  suggested_qty: number
  priority: number
  zone: DbrFeederZone
  nfp_snapshot?: number | null
  target_qty_snapshot?: number | null
  kit_force: boolean
  kit_shortage_qty: number
  source_schedule_id?: number | null
  reason_json?: {
    is_complete?: boolean
    missing_reasons?: string[]
    generator?: string
  } | null
  refreshed_at?: string | null
  cancelled_at?: string | null
}

export type DbrFeederSignalPreviewRow = {
  position_id: number
  item_id: number
  item_code: string
  warehouse_ref1c: string
  zone: DbrFeederZone
  priority: number
  nfp: number
  target_qty: number
  kit_force: boolean
  kit_shortage_qty: number
  suggested_qty: number
  is_complete: boolean
  missing_reasons: string[]
  action: 'open' | 'update' | 'cancel' | 'none' | string
}

export type DbrFeederSignalPreview = {
  schedule_id?: number | null
  positions: number
  actionable: number
  rows: DbrFeederSignalPreviewRow[]
  created?: number
  updated?: number
  reopened?: number
  cancelled?: number
}

export type DbrFeederSignalFilters = {
  status?: string
  zone?: string
  search?: string
  limit?: number
  offset?: number
}
