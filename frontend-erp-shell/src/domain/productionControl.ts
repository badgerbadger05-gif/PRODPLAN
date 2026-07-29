export type OrderRow = {
  product_id: number
  order_id?: number | null
  order_source?: string | null  // 'mrp' | '1c'
  order_ref1c?: string | null
  order_one_c_number?: string | null
  order_prodplan_number?: string | null
  item_id?: number | null
  order_number: string
  order_date?: string | null
  line_number?: number | string | null
  item_name: string
  item_article?: string | null
  item_code?: string | null
  unit?: string | null
  quantity: number
  produced_qty: number
  remaining_qty: number
  status: string
  coverage_status?: string | null
  coverage_label?: string | null
  material_coverage_status?: string | null
  material_coverage_label?: string | null
  material_coverage_calculated_at?: string | null
  issue_status?: string | null
  issue_count?: number
  workshop_name?: string | null
  stage_name?: string | null
  planned_start_date?: string | null
  planned_finish_date?: string | null
  forecast_date?: string | null
  forecast_shift_days?: number | null
  forecast_reason?: string | null
  route_sheet_printed_at?: string | null
  comment?: string | null
  optimal_batch?: number | null
  source?: string | null
  source_run_id?: number | null
  source_plan_id?: number | null
  source_plan_name?: string | null
  source_plan_period_from?: string | null
  source_plan_period_to?: string | null
  source_planned_order_id?: number | null
  source_mrp_requirement_id?: number | null
  source_mrp_allocation_key?: string | null
  mrp_req_net_qty?: number | null
  mrp_req_covered_qty?: number | null
  mrp_req_remaining_qty?: number | null
  // DBR: чем вызван запуск строки и какой полкой он ограничен.
  // См. ProductionOrderJournalRowResponse в backend/app/routers/production_control.py.
  launch_source?: LaunchSource
  shelf_warehouse_ref1c?: string | null
  shelf_pull_qty?: number | null
  shelf_materialized_qty?: number | null
  shelf_latest_start_date?: string | null
  paint_weld_chain?: PaintWeldChainInfo | null
}

// Источник запуска строки журнала: вытягивание с полки DBR либо остаток MRP.
// Значения приходят готовыми (LAUNCH_SOURCE_SHELF / LAUNCH_SOURCE_MRP в
// backend/app/services/production_control_journal.py), frontend их не выводит.
export type LaunchSource = 'shelf_pull' | 'mrp_remaining'

export function isShelfPullRow(row: Pick<OrderRow, 'launch_source'>) {
  return row.launch_source === 'shelf_pull'
}

// Цепочка «окраска↔сварка»: строка входит в связанную пару заказов.
export type PaintWeldChainInfo = {
  role: 'painted' | 'welded'
  link_id: number
  counterpart_order_id?: number | null
  counterpart_product_id?: number | null
}

// Фактический контракт закрытия цепочки:
// backend/app/services/paint_weld_chain.py::close_paint_chain,
// backend/app/routers/paint_weld.py::chain_close.
export type PaintWeldChainSide = 'weld' | 'paint'

export type PaintWeldChainCloseStatus = 'ok' | 'partial'

export type PaintWeldChainState =
  | 'not_started'
  | 'partially_posted'
  | 'manufactures_posted'
  | 'manufactures_posted_piecework_pending'
  | 'closed'

export type PaintWeldChainSidePlan = {
  product_id: number
  order_id: number
  remaining_qty: number
  qty_to_produce: number
  existing_manufacture_id?: number | null
  manufacture_id?: number
  manufacture_ref1c?: string | null
  produce?: ProduceLineResult | null
  error?: string
}

// Комбинированный СдельныйНаряд цепочки: export_chain_piecework_to_1c.
export type ChainPieceworkExportResult = {
  status: 'ok' | 'existing' | 'error' | 'partial_error'
  dry_run: boolean
  entity: string
  combined: boolean
  weld_manufacture_id: number
  paint_manufacture_id: number
  target_ref_key?: string | null
  reason?: string
  error?: string
  created?: number
  errored?: number
  skipped_rows: Array<Record<string, unknown>>
  entries?: Array<Record<string, unknown>>
  payloads?: Array<Record<string, unknown>>
}

export type PaintWeldChainCloseResult = {
  status: PaintWeldChainCloseStatus
  dry_run: boolean
  initiated_by?: string | null
  chain_link_id: number
  weld: PaintWeldChainSidePlan
  paint: PaintWeldChainSidePlan
  chain_state: PaintWeldChainState
  resume_required: boolean
  posted_sides: PaintWeldChainSide[]
  pending_sides: PaintWeldChainSide[]
  // Готовое человекочитаемое объяснение частичного закрытия — backend владеет
  // формулировкой, frontend её не собирает.
  message?: string
  error?: string
  ledger_readback?: 'queued'
  manufactures_export?: ExportManufacturesResult
  piecework_export?: ChainPieceworkExportResult
  piecework_preview?: ChainPieceworkExportResult | null
}

export const paintWeldChainSideLabels: Record<PaintWeldChainSide, string> = {
  weld: 'сварка',
  paint: 'окраска',
}

export const paintWeldChainStateLabels: Record<PaintWeldChainState, string> = {
  not_started: 'Не начата',
  partially_posted: 'Проведена частично',
  manufactures_posted: 'Сборки запасов проведены',
  manufactures_posted_piecework_pending: 'Сборки проведены, наряда нет',
  closed: 'Закрыта',
}

export function paintWeldChainStateLabel(state: PaintWeldChainState) {
  return paintWeldChainStateLabels[state] ?? state
}

export function paintWeldChainSidesLabel(sides: PaintWeldChainSide[]) {
  return sides.map((side) => paintWeldChainSideLabels[side] ?? side).join(', ')
}

export type MaterialRow = {
  component_item_id: number
  item_name: string
  item_article?: string | null
  item_code?: string | null
  qty_per_unit: number
  available_qty: number
  required_qty: number
  missing_qty?: number
  unit?: string | null
  reserved_qty?: number
  reserved_for_order_qty?: number
  reserved_at_workshop_qty?: number
  reserved_in_transit_qty?: number
  reserved_orders?: Array<{
    product_id: number
    order_id?: number | null
    order_number?: string | null
    order_ref1c?: string | null
    item_name?: string | null
    reserved_qty: number
    reserved_at_workshop_qty?: number
    reserved_in_transit_qty?: number
  }>
  availability_status?: string | null
  coverage_status?: string | null
  coverage_label?: string | null
  expected_dates?: Array<{ source: string; order_number?: string; ref?: string; date?: string; qty?: number }>
  eta_dates?: Array<{ source: string; ref?: string; date?: string; qty?: number }>
}

export type OrdersResponse = {
  rows: OrderRow[]
  total: number
  limit: number
  offset: number
  latest_run_id?: number | null
  latest_source_plan_id?: number | null
}

export type EmployeeOption = {
  employee_id: number
  employee_ref1c: string
  employee_type?: 'employee' | 'brigade' | string
  employee_code?: string | null
  employee_name: string
}

export type ProductionOperationOption = {
  line_number: number
  spec_id?: number | null
  spec_ref1c?: string | null
  spec_operation_id: number
  operation_id: number
  operation_ref1c?: string | null
  operation_name?: string | null
  stage_id?: number | null
  stage_name?: string | null
  time_norm?: number | null
}

export type ProductionOperationsResponse = {
  rows: ProductionOperationOption[]
  total: number
}

export type EmployeesResponse = {
  rows: EmployeeOption[]
  total: number
}

export type MaterialsResponse = {
  order_number?: string
  item_name?: string
  item_article?: string
  coverage_status?: string
  coverage_label?: string
  components: MaterialRow[]
}

export type ControlWarehouse = {
  warehouse_ref1c: string
  warehouse_code?: string | null
  warehouse_name?: string | null
  is_selected?: boolean
}

export type WorkshopWarehouse = {
  resource_id?: number
  workshop_id?: number
  workshop_name?: string | null
  warehouse_ref1c: string
  production_warehouse_ref1c?: string | null
}

export type IgnoredWarehouse = {
  warehouse_ref1c: string
  reason?: string | null
}

export type ControlSettings = {
  warehouses: ControlWarehouse[]
  workshop_warehouses: WorkshopWarehouse[]
  ignored_warehouses: IgnoredWarehouse[]
}

export type WarehouseCandidate = {
  ref1c: string
  name: string
  components_covered?: number
  total_components?: number
  qty?: number
}

export type MaterialIssueCreateResponse = {
  status: string
  created: Array<{
    issue_id: number
    document_number: string
    product_id: number
    order_number?: string
    item_name?: string
    lines_count?: number
    source_warehouse_ref1c?: string | null
    direction?: string
    warehouse_candidates?: WarehouseCandidate[]
  }>
  reused?: Array<{
    issue_id: number
    document_number: string
    product_id: number
    order_number?: string
    item_name?: string
    status?: string
    source_warehouse_ref1c?: string | null
    direction?: string
  }>
  selection_required?: Array<{
    product_id: number
    order_number?: string
    item_name?: string
    warehouse_candidates: WarehouseCandidate[]
    components?: Array<{
      component_item_id: number
      item_name: string
      item_article?: string | null
      required_qty: number
      warehouse_candidates: WarehouseCandidate[]
    }>
  }>
  already_on_destination?: Array<{
    product_id: number
    order_number?: string
    item_name?: string
    warehouse_ref1c?: string | null
    components?: Array<{
      component_item_id: number
      item_name: string
      item_article?: string | null
      required_qty: number
      covered_qty: number
      remaining_qty: number
      warehouse_ref1c?: string | null
    }>
  }>
  errors: string[]
}

export type MaterialIssueCreatePayload = {
  product_ids: number[]
  initiated_by?: string | null
  warehouse_ref1c?: string | null
  source_warehouse_ref1c?: string | null
}

export type TransferIssueRow = {
  issue_id: number
  document_number: string
  status: string
  direction?: string
  product_id: number
  order_id: number
  order_number: string
  order_prodplan_number?: string | null
  order_one_c_number?: string | null
  order_source?: string | null
  order_ref1c?: string | null
  item_id?: number | null
  item_name: string
  item_article?: string | null
  item_code?: string | null
  quantity: number
  remaining_qty: number
  unit?: string | null
  warehouse_ref1c?: string | null
  destination_warehouse_name?: string | null
  source_warehouse_ref1c?: string | null
  source_warehouse_name?: string | null
  exported_ref1c?: string | null
  one_c_number?: string | null
  exported_at?: string | null
  created_at?: string | null
  export_error?: string | null
  can_assemble?: boolean
  assemble_disabled_reason?: string | null
  line_status?: string | null
  issue_status?: string | null
  lines_count?: number
}

export type TransferIssuesResponse = {
  rows: TransferIssueRow[]
  total: number
  limit: number
  offset: number
  source_warehouses?: Array<{
    warehouse_ref1c: string
    warehouse_name?: string | null
  }>
}

export type MaterialIssueDetail = TransferIssueRow & {
  initiated_by?: string | null
  lines: Array<{
    line_id: number
    component_item_id: number
    item_code?: string | null
    item_name: string
    item_article?: string | null
    required_qty: number
    issued_qty: number
    unit?: string | null
    line_status?: string | null
  }>
}

export type ProductionOrderFilters = {
  search: string
  status: string
  workshop_id: string
  coverage_status: string
  root_item_id: string
  sort_by: 'planned_start_date'
  sort_dir: 'asc' | 'desc'
}

export type ProduceLinePayload = {
  qty: number
  executor?: string | null
  operation_executors?: Array<{
    line_number?: number
    spec_operation_id?: number
    operation_id?: number
    employee_ref1c?: string
    employee_name?: string
  }>
  comment?: string | null
}

export type ProduceLineResult = {
  status: string
  manufacture_id: number
  product_id: number
  order_id: number
  qty: number
  produced_qty_total: number
  remaining_qty: number
  commanded_qty_total: number
  command_remaining_qty: number
  fact_pending: boolean
  line_status: string
  ledger_readback: 'queued'
  manufacture_export: ExportManufacturesResult
  piecework_export: ExportPieceworkResult
}

export type ReturnLeftoversResult = {
  status: string
  issued_issues: number
  created_issues: number
  skipped_rows: Array<{ issue_id?: number; reason?: string }>
  entries: Array<{
    product_id: number
    issue_id: number
    direction: string
    issued_qty: number
    returned_qty: number
    warehouse_ref1c?: string | null
    source_warehouse_ref1c?: string | null
    detail?: string
  }>
}

export type ExportManufacturesResult = {
  status: string
  manufactures_eligible: number
  manufactures_created: number
  manufactures_existing: number
  manufactures_already_linked: number
  manufactures_error: number
  payloads: Array<Record<string, unknown>>
  skipped_rows: Array<Record<string, unknown>>
  entries: Array<Record<string, unknown>>
}

export type ExportPieceworkResult = {
  status: string
  manufactures_eligible: number
  manufactures_created: number
  manufactures_already_linked: number
  manufactures_error: number
  payloads: Array<Record<string, unknown>>
  skipped_rows: Array<Record<string, unknown>>
  entries: Array<Record<string, unknown>>
}

export type OrderQuantityPatchResponse = {
  status: string
  quantity: number
  remaining_qty: number
  mrp_req_net_qty?: number | null
  mrp_req_covered_qty?: number | null
  mrp_req_remaining_qty?: number | null
}

export type OrderStatePatchPayload = {
  status?: string
  issue_status?: string
  workshop_id?: number
  planned_start_date?: string
  planned_finish_date?: string
  comment?: string
}

export type SyncPostedTransfersResponse = {
  status: string
  candidates: number
  advanced: number
  errors: string[]
}

export type ProductionFilters = ProductionOrderFilters

export const productionStatusOptions = [
  ['shortage', 'Дефицит'],
  ['to_move', 'К перемещению'],
  ['ready', 'В работу'],
  ['in_progress', 'В работе'],
  ['done', 'Готов'],
  ['completed', 'Завершён'],
] as const

export const coverageLabels: Record<string, string> = {
  unknown: 'Неизвестно',
  shortage: 'Дефицит',
  partial: 'Частично',
  ready: 'Обеспечен',
  to_move: 'К перемещению',
  assembled: 'Собрано',
  in_progress: 'В работе',
  done: 'Готов',
  produced_partial: 'Готов',
  produced: 'Готов',
  production_error: 'Ошибка выпуска',
  completed: 'Завершён',
}

export function productionStatusLabel(value: string) {
  return coverageLabels[value] ?? value
}

export function productionStatusSelectValue(value: string) {
  if (value === 'partial') return 'shortage'
  if (value === 'assembled') return 'ready'
  if (value === 'produced' || value === 'produced_partial') return 'done'
  return productionStatusOptions.some(([status]) => status === value) ? value : ''
}
