export type OrderRow = {
  product_id: number
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
  expected_dates?: Array<{ source: string; order_number?: string; date?: string; qty?: number }>
}

export type OrdersResponse = {
  rows: OrderRow[]
  total: number
  limit: number
  offset: number
  latest_run_id?: number | null
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
  resource_id: number
  warehouse_ref1c: string
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

export type MaterialIssueCreateResponse = {
  status: string
  created: Array<{
    issue_id: number
    document_number: string
    product_id: number
    order_number?: string
    item_name?: string
    lines_count?: number
  }>
  errors: string[]
}

export const productionStatuses = [
  ['new', 'Новый'],
  ['opened', 'Открыт'],
  ['in_work', 'В работе'],
  ['waiting_materials', 'Ждет материалы'],
  ['done', 'Готов'],
  ['cancelled', 'Отменен'],
] as const

export const coverageLabels: Record<string, string> = {
  unknown: 'Неизвестно',
  shortage: 'Дефицит',
  partial: 'Частично',
  ready: 'Обеспечен',
  to_move: 'К перемещению',
  assembled: 'Собран',
  produced_partial: 'Произведен частично',
  produced: 'Произведен',
}

export type ProductionFilters = {
  search: string
  status: string
  workshop_id: string
  date_from: string
  date_to: string
}

export function productionStatusLabel(value: string) {
  return productionStatuses.find((row) => row[0] === value)?.[1] ?? value
}
