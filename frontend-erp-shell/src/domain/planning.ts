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
  snapshot_id: number | null
  ledger_generation: number | null
  cutoff: string | null
  truth_status: string
  truth_reason?: string | null
  run?: PlanningRunRow
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
  snapshot_total_qty?: {
    production?: number
    purchase?: number
    rework?: number
    capacity?: number
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
  forecast_status?: 'early' | 'on_time' | 'delayed' | 'critical' | 'unavailable'
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
  supplier_coverage_pct?: number | null
  supplier_coverage_status?: 'full' | 'partial' | 'none' | 'not_required'
  supplier_coverage_label?: string | null
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  main_area_id?: number | null
  main_area_name?: string | null
  main_stage_id?: number | null
  main_stage_name?: string | null
  badge?: string | null
  supplier_ref1c?: string | null
  supplier_name?: string | null
  category_id?: number | null
  category_name?: string | null
  category_ref1c?: string | null
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
  capacity_status: 'overloaded' | 'within_capacity'
}

export type MrpPagedResponse<T> = {
  snapshot_id: number | null
  ledger_generation: number | null
  cutoff: string | null
  truth_status: string
  truth_reason?: string | null
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
  status: 'draft' | 'fixed' | 'closed'
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
  execution_completed_qty?: number | null
  execution_base_qty?: number | null
  execution_pct?: number | null
  execution_partial?: boolean
  execution_progress_status?: 'unavailable' | 'not_started' | 'in_progress' | 'complete' | 'lower_bound'
  execution_status?: string | null
  execution_reason?: string | null
  execution_by_flow?: Record<string, {
    completed_qty: number
    base_qty: number
    execution_pct: number | null
    confirmed_pct?: number | null
    total_base_qty?: number
    available: boolean
  }> | null
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
    forecast_status?: 'early' | 'on_time' | 'delayed' | 'critical' | 'unavailable'
  }>
}

export type PeriodPlanMatrix = {
  plan: PeriodPlan
  buckets: string[]
  rows: PeriodPlanMatrixRow[]
  bucket_totals: Record<string, number>
  grand_total: number
  total_qty: number
  total: number
}

export type ExecutionWorkItem = components['schemas']['ExecutionJournalWorkItem']
export type ExecutionJournalLedgerLinkEvent = components['schemas']['ExecutionJournalLedgerEvent']
export type ExecutionJournalLedgerLinks = components['schemas']['ExecutionJournalLedgerLinks']
type ApiExecutionJournalRow = components['schemas']['ExecutionJournalRow']
export type ExecutionJournalRow = Omit<ApiExecutionJournalRow,
  'status' | 'root_item_ids' | 'information_links' | 'reservation_ids' | 'execution_events' | 'execution_allocations'
> & {
  status?: JournalRowStatus
  root_item_ids?: ApiExecutionJournalRow['root_item_ids']
  information_links?: ApiExecutionJournalRow['information_links']
  reservation_ids?: ApiExecutionJournalRow['reservation_ids']
  execution_events?: ApiExecutionJournalRow['execution_events']
  execution_allocations?: ApiExecutionJournalRow['execution_allocations']
}

export type JournalRowStatus = 'net_zero' | 'covered' | 'partial' | 'ordered' | 'none' | 'execution_unavailable'

export function journalRowStatus(row: Pick<ExecutionJournalRow, 'status' | 'net_qty' | 'remaining_qty' | 'completed_qty' | 'ordered_qty'>): JournalRowStatus {
  if (row.status) return row.status
  return 'execution_unavailable'
}

export function journalRowStatusLabel(status: JournalRowStatus) {
  if (status === 'execution_unavailable') return 'Исполнение недоступно'
  if (status === 'covered') return 'Закрыто'
  if (status === 'partial') return 'Частично'
  if (status === 'ordered') return 'Оформлено'
  if (status === 'none') return 'Не оформлено'
  return 'Покрыто складом'
}

export function journalRowStatusClass(status: JournalRowStatus) {
  if (status === 'execution_unavailable') return 'unavailable'
  if (status === 'covered') return 'ready'
  if (status === 'partial') return 'partial'
  if (status === 'ordered') return 'to_move'
  if (status === 'none') return 'shortage'
  return 'completed'
}

type ApiExecutionJournalSummary = components['schemas']['ExecutionJournalSummary']
export type ExecutionJournalSummary = Omit<ApiExecutionJournalSummary,
  'truth_status' | 'fully_covered' | 'partially_covered' | 'not_covered' | 'net_zero'
> & {
  truth_status?: ApiExecutionJournalSummary['truth_status']
  fully_covered: number
  partially_covered: number
  not_covered: number
  net_zero: number
}
export type ExecutionJournalResponseFacets = NonNullable<components['schemas']['ExecutionJournalResponse']['facets']>

export type PlanningTruthStatus = 'accepted' | 'unavailable' | 'stale' | 'uninitialized' | 'rejected'

export type ExecutionJournalTruthMeta = components['schemas']['ExecutionJournalTruthMeta']
type ApiExecutionJournalResponse = components['schemas']['ExecutionJournalResponse']
export type ExecutionJournalResponse = Omit<ApiExecutionJournalResponse, 'plan' | 'rows' | 'summary'> & {
  plan: PeriodPlan
  rows: ExecutionJournalRow[]
  summary: ExecutionJournalSummary
}

export function isPlanningTruthAccepted(value: Pick<ExecutionJournalResponse, 'truth_status'> | null | undefined) {
  return value?.truth_status === 'accepted'
}

export function periodPlanStatusLabel(status: string) {
  if (status === 'draft') return 'Черновик'
  if (status === 'fixed') return 'Зафиксирован'
  if (status === 'closed') return 'Закрыт'
  return status
}

export function periodPlanStatusClass(status: string) {
  if (status === 'draft') return 'running'
  if (status === 'fixed') return 'success'
  if (status === 'closed') return 'success'
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

import type { components } from '../lib/apiTypes'
