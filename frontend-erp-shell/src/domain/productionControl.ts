import type { components } from '../lib/apiTypes'

type ApiSchemas = components['schemas']

export type OrderRow = Omit<ApiSchemas['ProductionOrderJournalRowResponse'], 'paint_weld_chain' | 'paint_weld_pair' | 'selection_disabled_reason'> & {
  materialized_order_qty?: number | null
  launchable_qty?: number | null
  paint_weld_chain?: PaintWeldChainInfo | null
  paint_weld_pair?: PaintWeldPairInfo | null
  selection_disabled_reason?: string | null
}
export type OrdersResponse = ApiSchemas['ProductionOrderJournalResponse']
export type TruthMeta = OrdersResponse['truth_meta']

// Цепочка «окраска↔сварка»: строка входит в связанную пару заказов.
export type PaintWeldChainInfo = {
  // API types are regenerated separately and currently expose this as string.
  // Keep the known roles explicit while accepting an unknown backend value.
  role: 'painted' | 'welded' | (string & {})
  link_id: number
  counterpart_order_id?: number | null
  counterpart_product_id?: number | null
  counterpart_order_number?: string | null
  counterpart_order_prodplan_number?: string | null
  counterpart_item_name?: string | null
  counterpart_item_article?: string | null
  counterpart_item_code?: string | null
  counterpart_quantity?: number | null
  counterpart_remaining_qty?: number | null
  counterpart_unit?: string | null
  counterpart_workshop_name?: string | null
}

export type PaintWeldPairInfo = {
  pair_id: number
  role: 'painted' | 'welded'
  counterpart_item_id: number
  counterpart_item_name: string
  counterpart_item_article: string
  counterpart_item_code: string
  selection_disabled_reason?: string | null
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

export type EmployeeOption = ApiSchemas['ProductionEmployeeOptionResponse']

export type ProductionOperationOption = ApiSchemas['ProductionOperationOptionResponse']
export type ProductionOperationsResponse = ApiSchemas['ProductionOperationsResponse']
export type EmployeesResponse = ApiSchemas['ProductionEmployeeListResponse']

export type MaterialsResponse = Omit<ApiSchemas['ProductionMaterialsResponse'], 'components'> & {
  components: MaterialRow[]
  coverage_basis_item_name?: string | null
  coverage_basis_item_article?: string | null
  coverage_basis_item_code?: string | null
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

export type ProduceLinePayload = {
  qty?: number
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
  overproduced_qty: number
  order_quantity_before: number
  order_quantity_after: number
  line_status: string
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

export const productionStatusOptions = [
  ['not_created', 'Не создан'],
  ['created', 'Создан'],
  ['to_move', 'К перемещению'],
  ['ready', 'В работу'],
  ['in_progress', 'В работе'],
  ['done', 'Готов'],
  ['completed', 'Завершён'],
] as const

export const productionStatusLabels: Record<string, string> = {
  not_created: 'Не создан',
  created: 'Создан',
  shortage: 'Создан',
  partial: 'Создан',
  ready: 'В работу',
  to_move: 'К перемещению',
  assembled: 'В работу',
  in_progress: 'В работе',
  done: 'Готов',
  produced_partial: 'Готов',
  produced: 'Готов',
  production_error: 'Ошибка выпуска',
  completed: 'Завершён',
  cancelled: 'Отменён',
}

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

export type ProductionFilters = {
  search: string
  status: string
  workshop_id: string
  coverage_status: string
  root_item_id: string
  planning_contour: '' | 'mrp'
  sort_by: 'planned_start_date'
  sort_dir: 'asc' | 'desc'
}

export function productionStatusLabel(value: string) {
  return productionStatusLabels[value] ?? value
}

export function productionStatusSelectValue(value: string) {
  if (value === 'shortage' || value === 'partial') return 'created'
  if (value === 'assembled') return 'ready'
  if (value === 'produced' || value === 'produced_partial') return 'done'
  return productionStatusOptions.some(([status]) => status === value) ? value : ''
}
