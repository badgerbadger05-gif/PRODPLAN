export type OrderRow = {
  product_id: number
  order_id?: number | null
  order_source?: string | null  // 'mrp' | '1c'
  order_ref1c?: string | null
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
  source_planned_order_id?: number | null
  source_mrp_requirement_id?: number | null
  source_mrp_allocation_key?: string | null
  mrp_req_net_qty?: number | null
  mrp_req_covered_qty?: number | null
  mrp_req_remaining_qty?: number | null
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
}

export type EmployeeOption = {
  employee_id: number
  employee_ref1c: string
  employee_code?: string | null
  employee_name: string
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
  components_covered: number
  total_components: number
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
    warehouse_candidates?: WarehouseCandidate[]
  }>
  reused?: Array<{
    issue_id: number
    document_number: string
    product_id: number
    order_number?: string
    item_name?: string
    status?: string
  }>
  errors: string[]
}

export type TransferIssueRow = {
  issue_id: number
  document_number: string
  status: string
  direction?: string
  product_id: number
  order_id: number
  order_number: string
  order_ref1c?: string | null
  item_id?: number | null
  item_name: string
  item_article?: string | null
  item_code?: string | null
  quantity: number
  remaining_qty: number
  unit?: string | null
  warehouse_ref1c?: string | null
  source_warehouse_ref1c?: string | null
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
  completed: 'Завершён',
}

export type ProductionFilters = {
  search: string
  status: string
  workshop_id: string
  date_from: string
  date_to: string
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
