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
  /** Immutable MRP/Ledger tuple captured when the program was created. */
  source_run_id?: number | null
  ledger_generation_id?: number | null
  freeze_version?: number | null
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
  /** Explicitly selected FIXED_SNAPSHOT MRP run; never inferred by the UI. */
  source_run_id: number
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
  produced_qty?: number | null
  kit_status: DbrKitStatus
  kit_gate_status?: string | null
  execution_status?: string | null
  release_status?: string | null
  // Emitted once a slot is materialized into a 1С order (may be absent if the
  // board projection does not surface it — fall back to release_status).
  one_c_order_number?: string | null
  one_c_order_ref?: string | null
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
  green: number | null
  yellow: number | null
  red: number | null
  unknown: number | null
  slots: number
  plan_qty: number
  fact_qty: number | null
  kit_gate_status?: string | null
  execution_status?: string | null
}

export type DbrBoardMeta = {
  snapshot_id?: number | null
  ledger_generation?: number | null
  cutoff?: string | null
  runs: Array<{ run_id: number; freeze_version: number }>
  read_only?: boolean
  unavailable_sections?: string[] | Record<string, string | null | undefined>
  truth_status?: string | null
  truth_reason?: string | null
}

export type DbrBoard = {
  meta: DbrBoardMeta
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

// ── Materialization into 1С (Фаза 3) ─────────────────────────────────────────
// The 1С document payload is an opaque dict of 1С field names — shown only as a
// collapsible raw preview; the human summary is built from the slot/signal.
export type DbrOneCPayload = Record<string, unknown>

// Result of POST /drum/slots/{id}/release (dry_run preview or real write).
export type DbrReleaseResult = {
  ok: boolean
  dry_run: boolean
  kind: 'drum_slot' | string
  slot_id: number
  entity: string
  number: string
  payload?: DbrOneCPayload
  created: boolean
  already_released?: boolean
  release_status?: string
  one_c_order_ref?: string | null
  error?: string | null
  note?: string
}

// Per-slot entry inside a release-day report: either a full release result or a
// refusal (conflict / error) that never rolled back the slots that succeeded.
export type DbrReleaseDaySlotResult = DbrReleaseResult & {
  conflict?: string
  detail?: unknown
  error?: string | null
}

export type DbrReleaseDayResult = {
  ok: boolean
  dry_run: boolean
  schedule_id: number
  day: string
  slots_total: number
  released: number
  previews: number
  errors: number
  results: DbrReleaseDaySlotResult[]
}

// Result of POST /feeder/signals/{id}/launch (dry_run preview or real write).
export type DbrSignalLaunchResult = {
  ok: boolean
  dry_run: boolean
  kind: 'feeder_signal' | string
  signal_id: number
  entity: string
  number: string
  payload?: DbrOneCPayload
  created: boolean
  already_launched?: boolean
  status?: string
  one_c_order_ref?: string | null
  error?: string | null
  note?: string
}

// Structured 409 body carried by ApiError.detail when a launch is blocked by a
// material deficit.
export type DbrLaunchConflictDetail = {
  message?: string
  material_status?: string | null
  deficit_lines?: DbrKitLine[]
}

// ── Purchasing materialization (Document_ЗаказПоставщику) ─────────────────────
export type DbrPurchaseOrderLine = {
  item_id: number
  item_ref1c: string
  item_name: string
  qty: number
  need_date?: string | null
  order_date?: string | null
  source_ids: number[]
}

export type DbrPurchaseOrderGroup = {
  supplier_ref1c: string
  number: string
  status?: string | null
  target_ref_key?: string | null
  error?: string | null
  lines: DbrPurchaseOrderLine[]
}

export type DbrPurchaseUnresolved = {
  signal_id?: number
  item_id: number
  item_name?: string | null
  missing_supplier: boolean
  missing_item_ref1c: boolean
}

export type DbrPurchaseAlreadyExported = {
  signal_id?: number
  item_id?: number
  one_c_order_ref?: string | null
  one_c_order_number?: string | null
}

// Shared shape of /feeder/purchase/launch and /purchase-plan/materialize.
export type DbrPurchaseLaunchResult = {
  ok: boolean
  dry_run: boolean
  kind: string
  entity: string
  source?: { kind: string; program_id?: number }
  orders_planned: number
  signals_total?: number
  items_total?: number
  unresolved: DbrPurchaseUnresolved[]
  already_exported: DbrPurchaseAlreadyExported[]
  orders_created: number
  errors?: number
  orders: DbrPurchaseOrderGroup[]
  note?: string
}

export type DbrPurchasePlanRow = {
  item_id: number
  item_code: string
  item_name: string
  supplier_ref1c?: string | null
  demand_qty: number
  stock_qty: number
  open_order_qty: number
  available_qty: number
  to_order_qty: number
  need_date?: string | null
  replenishment_time: number
  order_before?: string | null
  within_lead_time_threshold: boolean
}

export type DbrPurchasePlanPreview = {
  ok: boolean
  source: { kind: string; program_id?: number }
  lead_time_threshold_days: number
  rows: DbrPurchasePlanRow[]
  rows_to_order: number
  items_total: number
  warnings: string[]
}

// ── Saved purchase cockpit ─────────────────────────────────────────────────
// A purchase page is a read model, not a client-triggered planning command.
// The worker captures these rows together with one accepted Item Ledger
// generation.  The optional lineage fields deliberately stay explicit so the
// UI can show which frozen MRP input produced the obligation set.
export type DbrPurchaseCockpitMeta = {
  snapshot_id?: number | null
  ledger_generation?: number | null
  cutoff?: string | null
  runs: Array<{ run_id: number; freeze_version: number }>
  truth_status?: string | null
  truth_reason?: string | null
  read_only?: boolean
  formula?: string | null
}

export type DbrPurchaseCockpitCoverage = {
  source_kind: string
  source_ref?: string | null
  source_line_ref?: string | null
  pin_kind?: string | null
  alloc_qty: number
  covered_qty: number
  realized_qty: number
  evaporated_qty: number
}

export type DbrPurchaseCockpitObligation = {
  reservation_id: number
  requirement_id?: number | null
  priority_period_from?: string | null
  priority_period_to?: string | null
  outstanding_qty: number
  uncovered_qty: number
  coverage: DbrPurchaseCockpitCoverage[]
}

/** Ledger-native purchase axis.  Never substitute legacy plan demand or stock. */
export type DbrPurchaseCockpitRow = {
  item_id: number
  item_code: string
  item_name?: string | null
  item_ref1c?: string | null
  supplier_ref1c?: string | null
  article?: string | null
  unit?: string | null
  replenishment_time?: number | null
  warehouse_ref1c: string
  planning_stock_pool: string
  need_date?: string | null
  reservation_ids: number[]
  obligations: DbrPurchaseCockpitObligation[]
  outstanding_obligation_qty: number
  uncovered_qty: number
  to_order_qty: number
  stock_qty: number
  exact_future_supply_qty: number
  excluded_future_supply?: Array<{
    supply_kind: string
    source_ref?: string | null
    source_line_ref?: string | null
    evidence_status?: string | null
    destination_warehouse_ref1c?: string | null
    reason?: string | null
  }>
}

export type DbrPurchaseCockpit = {
  meta: DbrPurchaseCockpitMeta
  rows: DbrPurchaseCockpitRow[]
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
export type DbrFeederSignalStatus = 'Open' | 'Diagnostic' | 'Cancelled' | string

// ── Material readiness (Фаза 3.1) ─────────────────────────────────────────────
// Kit-line class: 'ok' (Готов), 'part' (Частично), 'no' (Дефицит), 'q' (Расписан выше).
export type DbrKitLineCls = 'ok' | 'part' | 'no' | 'q' | string
// Boundary kind of a kit line: 'make' (производимая) / 'buy' (закупная).
export type DbrKitBoundaryKind = 'make' | 'buy' | string
// Material status of a queue signal.
export type DbrMaterialStatus = 'Готов' | 'Частично' | 'Дефицит' | 'Расписан выше' | string

export type DbrKitLine = {
  item: string
  item_name: string
  article: string
  need: number
  have: number
  gross: number
  kind: DbrKitBoundaryKind
  level: string
  cls: DbrKitLineCls
  buffered: boolean
}

export type DbrRootItem = {
  item: string
  item_name: string
  article: string
}

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
  // Chain pegging (Фаза 3.2): a chain child points at its parent signal and
  // carries a depth > 0; queue heads have chain_depth 0 and no parent.
  parent_signal_id?: number | null
  chain_depth?: number
  source_schedule_id?: number | null
  drum_slot_id?: number | null
  need_date?: string | null
  required_date?: string | null
  raw_demand_qty?: number | null
  raw_shortage_qty?: number | null
  calculated_batch_qty?: number | null
  data_quality?: string[]
  is_incomplete?: boolean
  // Material readiness annotations (Фаза 3.1), present on the queue listing.
  material_status?: DbrMaterialStatus | null
  kit_cls?: DbrKitLineCls | null
  can_launch?: boolean
  deficit_lines?: DbrKitLine[]
  root_items?: DbrRootItem[]
  reason_json?: {
    is_complete?: boolean
    missing_reasons?: string[]
    generator?: string
    parent_signal_id?: number
    chain_depth?: number
    shortfall?: number
  } | null
  refreshed_at?: string | null
  cancelled_at?: string | null
}

export type DbrFeederSignalPreviewRow = {
  signal_type: string
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
  missing_reasons?: string[]
  data_quality?: string[]
  slot_id?: number | null
  need_date?: string | null
  required_date?: string | null
  raw_demand_qty?: number | null
  raw_shortage_qty?: number | null
  calculated_batch_qty?: number | null
  action: 'open' | 'update' | 'cancel' | 'none' | string
}

export type DbrFeederSignalPreview = {
  schedule_id?: number | null
  positions: number
  actionable: number
  diagnostic?: number
  under_schedule_demands?: number
  rows: DbrFeederSignalPreviewRow[]
  created?: number
  updated?: number
  reopened?: number
  cancelled?: number
  diagnostic_persisted?: number
}

export type DbrFeederSignalFilters = {
  status?: string
  zone?: string
  signal_type?: string
  search?: string
  limit?: number
  offset?: number
}

// ── Material deficits aggregate (Фаза 3.1, design §5) ─────────────────────────
export type DbrFeederDeficit = {
  item: string
  item_name: string
  article: string
  source: 'make' | 'buy' | string
  short_qty: number
  need_sum: number
  gross: number
  blocks_signals: number
  nearest_due?: string | null
}

export type DbrFeederDeficitsResult = {
  deficits: DbrFeederDeficit[]
  kpis: {
    deficit_materials: number
    queue_open: number
    stock_source: string
  }
}

// ── Saved DBR feeder cockpit ────────────────────────────────────────────────
// The feeder page has one read boundary.  All four sections below are captured
// together by the backend worker for one accepted Item Ledger generation.
export type DbrCockpitUnavailableSections = string[] | Record<string, string | null | undefined>

export type DbrFeederCockpitMeta = {
  snapshot_id?: number | null
  policy_snapshot_id?: number | null
  ledger_generation?: number | null
  cutoff?: string | null
  truth_status?: string | null
  truth_reason?: string | null
  chain_enabled?: boolean
  read_only?: boolean
  unavailable_sections?: DbrCockpitUnavailableSections
}

export type DbrFeederCockpit = {
  meta: DbrFeederCockpitMeta
  positions?: DbrFeederPosition[]
  signals?: DbrFeederSignal[]
  deficits?: DbrFeederDeficitsResult | {
    items?: unknown[]
    source?: string
  }
  processing_board?: DbrProcessingBoard | { status: 'unavailable'; reason?: string }
}

// ── Давальческий контур переработки (питатель №3, фаза 4) ───────────────────
export type DbrProcessingOrder = {
  order_id: number
  line_id: number
  line_number?: number | null
  order_number: string
  order_date?: string | null
  transfer_date?: string | null
  report_date?: string | null
  stage: 'ordered' | 'transferred' | 'reported'
  remaining_qty: number
  age_days?: number | null
  overdue: boolean
}

export type DbrProcessingRow = {
  position_id: number
  item_id: number
  item_code: string
  item_article: string
  item_name: string
  adu: number
  rt_days: number
  trip_interval_days: number
  red_qty: number
  yellow_qty: number
  target_qty: number
  nfp?: number | null
  zone?: string | null
  penetration?: number | null
  stock_qty?: number | null
  open_supply_qty?: number | null
  chain_supply_qty?: number | null
  at_contractor_qty?: number | null
  is_complete?: boolean | null
  missing_reasons: string[]
  open_orders: DbrProcessingOrder[]
  has_overdue: boolean
  roundtrip_kpi?: DbrProcessingRoundtripKpi
}

export type DbrProcessingRoundtripKpi = {
  semantics: string
  eligible_rows: number
  completed_rows: number
  completed_orders: number
  completed_qty: number
  weighted_avg_days?: number | null
  max_days?: number | null
  within_roundtrip_rows: number
  within_roundtrip_qty: number
  invalid_date_rows: number
}

export type DbrProcessingContractorKpi = {
  supplier_id: number
  supplier_ref1c: string
  supplier_name: string
  roundtrip_kpi: DbrProcessingRoundtripKpi
}

export type DbrProcessingStockHealth = {
  status?: string
  last_attempt_at?: string | null
  last_success_at?: string | null
  last_error?: string | null
  rows_seen?: number
  rows_stored?: number
  unmatched_items?: number
  total_qty?: number
  [key: string]: unknown
}

export type DbrProcessingBoard = {
  roundtrip_limit_days: number
  roundtrip_kpi_semantics?: string
  positions: DbrProcessingRow[]
  contractors?: DbrProcessingContractorKpi[]
  processing_stock?: DbrProcessingStockHealth
  positions_total: number
  overdue_positions: number
  generated_at: string
}

export type DbrProcessingChainRow = {
  parent_signal_id: number
  parent_item?: string | null
  component_item?: string | null
  suggested_qty?: number | null
  shortage_qty?: number | null
  warehouse_ref1c?: string | null
  unresolved_reasons: string[]
}

export type DbrProcessingChainPreview = {
  read_only: true
  processing_open_signals: number
  netted_signals: number
  desired_children: number
  distinct_components: number
  parents_with_children: number
  children: DbrProcessingChainRow[]
  unresolved: DbrProcessingChainRow[]
  unresolved_count: number
}

export type DbrProcessingOrderPreview = {
  dry_run: true
  write_capable: false
  live_contract_confirmed: false
  gate: string
  entity: string
  signal_id: number
  payload: Record<string, unknown>
}

export type DbrProcessingTripManifestLine = {
  signal_id: number
  covered_item_code: string
  covered_item_name: string
  suggested_qty: number
  need_date?: string | null
  required_date?: string | null
  bare_item_code?: string | null
  bare_item_name?: string | null
  tolling_qty?: number | null
  unresolved_reasons: string[]
}

export type DbrProcessingTripManifest = {
  read_only: true
  processing_trip_interval_days: number
  signals_total: number
  contractors_total: number
  unresolved_count: number
  contractors: Array<{
    contractor_ref1c?: string | null
    contractor_name?: string | null
    lines: DbrProcessingTripManifestLine[]
  }>
}

// ── Chain explosion (Фаза 3.2) ────────────────────────────────────────────────
export type DbrChainPreviewItem = {
  item: string
  parents: number
  qty_sum: number
}

export type DbrChainPreview = {
  enabled: boolean
  open_signals: number
  level1_children: number
  distinct_items: number
  top_items: DbrChainPreviewItem[]
}

export type DbrChainRefresh = {
  created: number
  updated: number
  reopened: number
  revoked: number
  no_warehouse: number
  passes: number
  disabled?: boolean
}
