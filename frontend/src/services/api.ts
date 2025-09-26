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
} = {}): Promise<{ rows: any[]; total: number; limit: number; offset: number }> {
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
} = {}): Promise<{ rows: any[]; total: number; limit: number; offset: number }> {
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