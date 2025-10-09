import { api } from '../boot/axios'

// Используем единый экземпляр axios из boot (baseURL='/api', таймауты настроены там)

export interface ApiResponse<T> {
  data: T
  success: boolean
  message?: string
}

export default api
// Specification Tree API typing and helper

export type SpecStage = {
  id: string | number
  name: string
} | null

export type SpecOperationInfo = {
  id: string | number | null
  name: string | null
} | null

export type SpecComputed = {
  treeQty?: number | null
  treeTimeNh?: number | null
}

export type SpecNode = {
  id: string
  parentId: string | null
  type: 'item' | 'operation'
  name: string | null
  article: string | null
  stage: SpecStage
  operation: SpecOperationInfo
  qtyPerParent: number | null
  unit: string | null
  replenishmentMethod?: string | null
  timeNormNh: number | null
  computed?: SpecComputed
  hasChildren: boolean
  warnings: string[]
  item?: { id: number; code: string }
  // For QTable tree
  children?: SpecNode[]
  __loading__?: boolean
}

export async function getSpecificationTree(params: {
  item_code?: string
  item_id?: number
  root_qty?: number
  parent_id?: string
  depth?: number
}): Promise<{ nodes: SpecNode[]; meta: any }> {
  const { data } = await api.get('/v1/specification/tree', { params })
  return data
}
export async function getSpecificationFull(params: {
  item_code?: string
  item_id?: number
  root_qty?: number
  max_depth?: number
}): Promise<{ nodes: SpecNode[]; meta: any }> {
  const { data } = await api.get('/v1/specification/full', { params })
  return data
}
// ===== MRP Planning API =====

export type PlanningRunRow = {
  run_id: number
  status: string
  started_at: string | null
  finished_at: string | null
  horizon_days?: number | null
  use_weekly?: boolean
  order_count?: number
  purchase_count?: number
  overload_buckets?: number
}

export type PlanningRunsResponse = {
  rows: PlanningRunRow[]
  total: number
  limit: number
  offset: number
}

export async function listPlanningRuns(params: {
  limit?: number
  offset?: number
} = {}): Promise<PlanningRunsResponse> {
  const { data } = await api.get('/v1/plan/runs', { params })
  return data
}

export async function startPlanningRun(body: {
  horizon_days?: number
  use_weekly?: boolean
  config_overrides?: any
  started_by?: string
} = {}): Promise<{ status: string; run_id: number }> {
  const { data } = await api.post('/v1/plan/calc', body || {})
  return data
}

export async function getPlanningRunSummary(runId: number): Promise<{
  run: any
  counts: any
  capacity: any
  kpi: any
  warnings: any[]
}> {
  const { data } = await api.get(`/v1/plan/results/${runId}`)
  return data
}

export async function getPlanningResultProduction(runId: number, params: {
  item_id?: number
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<{ rows: any[]; total: number; total_qty: number; limit: number; offset: number }> {
  const { data } = await api.get(`/v1/plan/results/${runId}/production`, { params })
  return data
}

export async function getPlanningResultPurchases(runId: number, params: {
  item_id?: number
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'order_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<{ rows: any[]; total: number; total_qty: number; limit: number; offset: number }> {
  const { data } = await api.get(`/v1/plan/results/${runId}/purchases`, { params })
  return data
}

export async function getPlanningResultCapacity(runId: number, params: {
  area_id?: number
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
} = {}): Promise<{ rows: any[]; total: number; limit: number; offset: number }> {
  const { data } = await api.get(`/v1/plan/results/${runId}/capacity`, { params })
  return data
}

export async function getPlanningResultPegging(runId: number, params: {
  child_item_id?: number
  parent_item_id?: number
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
} = {}): Promise<{ rows: any[]; total: number; limit: number; offset: number }> {
  const { data } = await api.get(`/v1/plan/results/${runId}/pegging`, { params })
  return data
}

// Planning Configs API
export async function listPlanningConfigs(params: { limit?: number; offset?: number } = {}): Promise<any> {
  const { data } = await api.get('/v1/plan/configs', { params })
  return data
}

export async function getActivePlanningConfig(): Promise<any> {
  const { data } = await api.get('/v1/plan/configs/active')
  return data
}

export async function createPlanningConfig(body: {
  config: any
  comment?: string
  created_by?: string
  activate?: boolean
}): Promise<any> {
  const { data } = await api.post('/v1/plan/configs', body)
  return data
}

export async function activatePlanningConfig(configId: number): Promise<any> {
  const { data } = await api.post(`/v1/plan/configs/${configId}/activate`, {})
  return data
}
export async function listItems(params: {
  limit?: number
  offset?: number
} = {}): Promise<{ rows: any[]; total: number; limit: number; offset: number }> {
  const limit = params.limit ?? 100
  const offset = params.offset ?? 0
  // Backend expects skip/limit and prefers trailing slash to avoid 307
  const { data } = await api.get('/v1/items/', { params: { skip: offset, limit } })
  if (Array.isArray(data)) {
    return { rows: data, total: data.length, limit, offset }
  }
  // If backend already returns {rows,total,limit,offset}
  const rows = (data?.rows ?? [])
  const total = (typeof data?.total === 'number') ? data.total : rows.length
  return { rows, total, limit, offset }
}

export async function listResources(params: {
  limit?: number
  offset?: number
} = {}): Promise<{ rows: any[]; total: number; limit: number; offset: number }> {
  // Backend returns an array; normalize to {rows,total,limit,offset}
  const { data } = await api.get('/v1/resources/')
  if (Array.isArray(data)) {
    const rows = data
    const total = rows.length
    return { rows, total, limit: total, offset: 0 }
  }
  const rows = (data?.rows ?? [])
  const total = (typeof data?.total === 'number') ? data.total : rows.length
  return { rows, total, limit: rows.length, offset: 0 }
}

/** Export production results as CSV or XLSX (base64) */
export async function exportPlanningResultProduction(runId: number, params: {
  format: 'csv' | 'xlsx'
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
}): Promise<any> {
  const { data } = await api.get(`/v1/plan/results/${runId}/production/export`, { params })
  return data
}

/** Export purchases results as CSV or XLSX (base64) */
export async function exportPlanningResultPurchases(runId: number, params: {
  format: 'csv' | 'xlsx'
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'order_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
}): Promise<any> {
  const { data } = await api.get(`/v1/plan/results/${runId}/purchases/export`, { params })
  return data
}
// === Backend-first grouped/agenda/summary API wrappers (additive) ===

export async function getPlanningResultProductionGrouped(runId: number, params: {
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  area_id?: number
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<{
  groups: Array<{
    area_id: number
    area_name: string
    orders: Array<{
      agg_key: string
      item_id: number
      item_name?: string
      item_article?: string
      unit?: string
      qty: number
      norm_hours_total: number
      norm_hours_per_unit?: number | null
    }>
    norm_sum_hours: number
    min_days_to_need?: number | null
    cap_overload_hours?: number
    cap_overloaded_buckets?: number
  }>
  total_groups: number
  total_orders: number
  limit: number
  offset: number
}> {
  const { data } = await api.get(`/v1/plan/results/${runId}/production/grouped`, { params })
  return data
}

export async function getPlanningResultProductionAgendaDay(runId: number, params: {
  day_date: string
  area_id?: number
}): Promise<{
  day: string
  groups: Array<{
    area_id: number
    area_name: string
    orders: Array<{
      agg_key: string
      item_id: number
      item_name?: string
      item_article?: string
      unit?: string
      qty: number      // выпуск за день по виду/участку (если нет перегруза)
      norm_hours_total: number // часы за день (если нет перегруза)
      norm_hours_per_unit?: number | null
      // расширения для перегруза
      order_id?: number
      display_qty?: number
      display_norm_hours_total?: number
      overload?: boolean
    }>
    norm_sum_hours: number
    sum_qty: number
    cap_overload_hours?: number
    // расширения по мощности на день
    hours_available_day?: number
    cap_overload_percent?: number | null
  }>
}> {
  const { data } = await api.get(`/v1/plan/results/${runId}/production/agenda_day`, { params })
  return data
}

export async function getPlanningResultPurchasesGrouped(runId: number, params: {
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
} = {}): Promise<{
  rows: Array<{
    agg_key: string
    item_id: number
    item_name?: string
    item_article?: string
    unit?: string
    qty: number
  }>
  total: number
  limit: number
  offset: number
}> {
  const { data } = await api.get(`/v1/plan/results/${runId}/purchases/grouped`, { params })
  return data
}

export async function getPlanningResultCapacitySummary(runId: number, params: {
  bucket_type?: 'daily' | 'weekly'
  date_from?: string
  date_to?: string
} = {}): Promise<{
  map: {
    [areaId: number]: {
      hours_planned: number
      hours_available: number
      overload_hours: number
      overloaded_buckets: number
    }
  }
  total_rows: number
}> {
  const { data } = await api.get(`/v1/plan/results/${runId}/capacity/summary`, { params })
  return data
}