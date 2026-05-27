export type PlanningRunRow = {
  run_id: number
  status: string
  started_at: string | null
  finished_at: string | null
  horizon_days?: number | null
  source_plan_id?: number | null
  source_plan_name?: string | null
  period_from?: string | null
  period_to?: string | null
  requirement_count?: number | null
  requirement_remaining_qty?: number | null
  order_count?: number | null
  purchase_count?: number | null
  overload_buckets?: number | null
}

export type PlanningRunsResponse = {
  rows: PlanningRunRow[]
  total: number
  limit: number
  offset: number
}

export type StartPlanningRunResponse = {
  status: string
  run_id: number
}

export type MrpSummary = {
  run: PlanningRunRow
  counts?: {
    production_orders?: number
    purchase_requests?: number
    rework_requests?: number
  }
  capacity?: {
    overloaded_buckets?: number
    overload_total?: number
    hours_planned_total?: number
    hours_available_total?: number
  }
  warnings?: Array<Record<string, unknown>>
}

export type MrpProductionRow = {
  order_id: number
  requirement_id?: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  requested_qty?: number | null
  planned_qty?: number | null
  covered_qty?: number | null
  remaining_qty?: number | null
  need_date?: string | null
  start_date?: string | null
  finish_date?: string | null
  forecast_date?: string | null
  forecast_shift_days?: number | null
  forecast_reason?: string | null
  main_area_id?: number | null
  main_area_name?: string | null
  main_stage_id?: number | null
  main_stage_name?: string | null
  norm_hours_total?: number | null
  badge?: string | null
  turning_blank_priority?: boolean
  source_order_ids?: number[]
}

export type MrpPurchaseRow = {
  purchase_id: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  requested_qty?: number | null
  supplier_covered_qty?: number | null
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  main_area_id?: number | null
  main_area_name?: string | null
  main_stage_id?: number | null
  main_stage_name?: string | null
  badge?: string | null
  late_supplier_order?: boolean
  turning_blank_priority?: boolean
  source_purchase_ids?: number[]
}

export type MrpReworkRow = {
  rework_id: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  requested_qty: number
  planned_qty: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  spec_code?: string | null
  spec_name?: string | null
  component_limit?: number | null
  component_blocked?: boolean
  component_partial?: boolean
  badge?: string | null
}

export type MrpCapacityRow = {
  area_id: number
  bucket_date?: string | null
  hours_planned: number
  hours_available: number
  overload_hours: number
}

export type MrpPagedResponse<T> = {
  rows: T[]
  total: number
  total_qty?: number
  limit: number
  offset: number
}

export function planningStatusLabel(status: string) {
  const value = status.toUpperCase()
  if (value === 'SUCCESS') return 'Успешно'
  if (value === 'RUNNING') return 'Выполняется'
  if (value === 'FAILED') return 'Ошибка'
  return status || 'Неизвестно'
}

// ── Period Plans ──────────────────────────────────────────────────────────────

export type PeriodPlan = {
  id: number
  name: string
  status: 'draft' | 'fixed' | 'archived'
  period_from: string
  period_to: string
  comment?: string | null
  created_by?: string | null
  fixed_at?: string | null
  fixed_by?: string | null
  created_at?: string | null
  updated_at?: string | null
  line_count?: number
  total_qty?: number
}

export type PeriodPlanListResponse = {
  rows: PeriodPlan[]
  total: number
}

export type PeriodPlanRun = {
  run_id: number
  status: string
  started_at?: string | null
  finished_at?: string | null
  started_by?: string | null
  horizon_days?: number | null
  period_from?: string | null
  period_to?: string | null
  fixed_at?: string | null
}

export type PeriodPlanRunsResponse = {
  rows: PeriodPlanRun[]
  total: number
}

export type PeriodPlanMatrixRow = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  total_qty: number
  buckets: Record<string, number>
  locked_buckets: Record<string, number>
  bucket_forecasts?: Record<string, {
    forecast_date?: string | null
    forecast_shift_days?: number | null
    forecast_reason?: string | null
  }>
}

export type PeriodPlanMatrix = {
  plan: PeriodPlan
  buckets: string[]
  rows: PeriodPlanMatrixRow[]
  total: number
}

export type ExecutionWorkItem = {
  type: 'production_order' | 'planned_order' | 'planned_purchase' | 'planned_rework'
  product_id?: number
  order_id?: number
  order_number?: string
  order_state?: string
  purchase_id?: number
  rework_id?: number
  qty: number
  completed_qty?: number
  remaining_qty?: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number
  forecast_date?: string | null
  forecast_shift_days?: number | null
  forecast_reason?: string | null
}

export type ExecutionJournalRow = {
  req_id: number
  item_id: number
  item_code: string
  item_article?: string | null
  item_name: string
  flow: 'production' | 'purchase' | 'rework'
  bom_level: number
  gross_qty: number
  stock_qty?: number
  net_qty: number
  ordered_qty: number
  completed_qty: number
  covered_qty: number
  remaining_qty: number
  unassigned_qty?: number
  coverage_pct: number
  forecast_date?: string | null
  forecast_shift_days?: number | null
  forecast_reason?: string | null
  work_items: ExecutionWorkItem[]
}

export type ExecutionJournalSummary = {
  total_items: number
  fully_covered: number
  partially_covered: number
  not_covered: number
  net_zero: number
}

export type ExecutionJournalResponse = {
  plan: PeriodPlan
  run_id: number
  rows: ExecutionJournalRow[]
  summary: ExecutionJournalSummary
}

export function periodPlanStatusLabel(status: string) {
  if (status === 'draft') return 'Черновик'
  if (status === 'fixed') return 'Зафиксирован'
  if (status === 'archived') return 'Архив'
  return status
}

export function periodPlanStatusClass(status: string) {
  if (status === 'draft') return 'running'
  if (status === 'fixed') return 'success'
  return ''
}

export function flowLabel(flow: string) {
  if (flow === 'production') return 'Производство'
  if (flow === 'purchase') return 'Закупка'
  if (flow === 'rework') return 'Переработка'
  return flow
}

export function flowClass(flow: string) {
  if (flow === 'production') return 'to_move'
  if (flow === 'purchase') return 'ready'
  if (flow === 'rework') return 'partial'
  return ''
}

export function coverageClass(pct: number) {
  if (pct >= 95) return 'ready'
  if (pct >= 50) return 'partial'
  return 'shortage'
}
