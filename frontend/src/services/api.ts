import { api } from '../boot/axios'
import type {
  PurchaseCategoryGroupedResponse,
  ReworkGroupedResponse,
  ReworkRow,
  PagedResponse,
} from '../types/mrp'

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
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'start_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<{ rows: any[]; total: number; total_qty: number; limit: number; offset: number }> {
  const { data } = await api.get(`/v1/plan/results/${runId}/production`, { params })
  return data
}

export async function getPlanningResultPurchases(runId: number, params: {
  item_id?: number
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
  date_from?: string
  date_to?: string
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'start_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
}): Promise<any> {
  const { data } = await api.get(`/v1/plan/results/${runId}/production/export`, { params })
  return data
}

/** Export purchases results as CSV or XLSX (base64) */
export async function exportPlanningResultPurchases(runId: number, params: {
  format: 'csv' | 'xlsx'
  date_from?: string
  date_to?: string
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'order_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
}): Promise<any> {
  const { data } = await api.get(`/v1/plan/results/${runId}/purchases/export`, { params })
  return data
}

/** Create 1C supplier orders from purchase planning results */
export async function exportPlanningResultPurchasesTo1C(runId: number, body: {
  date_from?: string
  date_to?: string
  purchase_ids?: number[]
  dry_run?: boolean
} = {}): Promise<any> {
  const { data } = await api.post(`/v1/plan/results/${runId}/purchases/export-to-1c`, body)
  return data
}

/** Export rework results as CSV or XLSX (base64) */
export async function exportPlanningResultRework(runId: number, params: {
  format: 'csv' | 'xlsx'
  date_from?: string
  date_to?: string
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'requested_qty' | 'planned_qty' | 'need_date' | 'order_date' | 'bucket_date' | 'priority_index' | 'spec_name'
  sort_dir?: 'asc' | 'desc'
}): Promise<any> {
  const { data } = await api.get(`/v1/plan/results/${runId}/rework/export`, { params })
  return data
}
// === Backend-first grouped/agenda/summary API wrappers (additive) ===

export async function getPlanningResultProductionGrouped(runId: number, params: {
  date_from?: string
  date_to?: string
  area_id?: number
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'start_date' | 'priority_index'
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


export async function getPlanningResultPurchasesGrouped(runId: number, params: {
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

export async function getPlanningResultPurchasesGroupedByCategory(runId: number, params: {
  item_id?: number
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'order_date' | 'bucket_date' | 'priority_index'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<PurchaseCategoryGroupedResponse> {
  const { data } = await api.get(`/v1/plan/results/${runId}/purchases/grouped-by-category`, { params })
  return data
}

export async function getPlanningResultRework(runId: number, params: {
  item_id?: number
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'requested_qty' | 'planned_qty' | 'need_date' | 'order_date' | 'bucket_date' | 'priority_index' | 'spec_name'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<PagedResponse<ReworkRow> & { total_qty: number }> {
  const { data } = await api.get(`/v1/plan/results/${runId}/rework`, { params })
  return data
}

export async function getPlanningResultReworkGroupedByCategory(runId: number, params: {
  item_id?: number
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
  sort_by?: 'item_name' | 'item_article' | 'qty' | 'need_date' | 'order_date' | 'bucket_date'
  sort_dir?: 'asc' | 'desc'
} = {}): Promise<ReworkGroupedResponse> {
  const { data } = await api.get(`/v1/plan/results/${runId}/rework/grouped-by-category`, { params })
  return data
}

export async function getPlanningResultCapacitySummary(runId: number, params: {
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
export interface ShortageReportResponse {
  status?: string;
  format?: string;
  data_base64?: string;
  filename?: string;
  total_rows?: number;
  message?: string;
}

export async function getShortageReport(runId: number): Promise<ShortageReportResponse> {
  const response = await api.get(`/v1/plan/results/${runId}/shortage-report`)
  return response.data
}

// ===== Weekly production report (week view + day close) =====

export type ProductionReportWeekDay = {
  date: string
  is_workday: boolean
  close_status?: string | null
  closed_planned?: number
  closed_fact?: number
  carry_qty?: number
}

export type ProductionReportWeekRow = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  plan_by_day: Record<string, number>
  fact_by_day: Record<string, number>
  // Diagnostics for day close / carry
  carry_by_day?: Record<string, number>
  closed_plan_by_day?: Record<string, number>
  closed_fact_by_day?: Record<string, number>
  plan_week: number
  fact_week: number
  remaining_week: number
}

export type ProductionReportWeekResponse = {
  week_start: string
  days: ProductionReportWeekDay[]
  rows: ProductionReportWeekRow[]
  close_hint?: {
    today: string
    close_date: string
    target_date: string
  }
}

export async function getProductionReportWeek(body: {
  week_start?: string
  any_date_in_week?: string
} = {}): Promise<ProductionReportWeekResponse> {
  const { data } = await api.post('/v1/plan/production_report/week', body)
  return data
}

export async function bulkUpsertProductionReportFact(body: {
  entries: Array<{ item_id: number; date: string; fact_qty: number }>
  rerun_editable_date?: string
}): Promise<{ status: string; saved: number }> {
  const { data } = await api.post('/v1/plan/production_report/fact/bulk_upsert', body)
  return data
}

export async function closeProductionReportDay(body: {
  close_date?: string | null
  closed_by?: string | null
} = {}): Promise<any> {
  const { data } = await api.post('/v1/plan/production_report/day/close', body)
  return data
}

// ===== Planning anchor (plan window start) =====

export type PlanningAnchorResponse = {
  today: string
  last_closed_date?: string | null
  anchor_date: string
}

export async function getPlanningAnchor(): Promise<PlanningAnchorResponse> {
  const { data } = await api.get('/v1/plan/anchor')
  return data
}

// ===== Forced orders (manual/override) =====

export interface ForcedOrderCreateRequest {
  run_id?: number | null
  item_id: number
  need_date: string // YYYY-MM-DD
  requested_qty: number
  created_by?: string | null
  reason?: string | null
}

export async function createForcedOrder(req: ForcedOrderCreateRequest): Promise<{ status: string; request_id: number }> {
  const { data } = await api.post('/v1/plan/forced_orders', req)
  return data
}

export async function processForcedOrder(requestId: number): Promise<any> {
  const { data } = await api.post(`/v1/plan/forced_orders/${requestId}/process`, {})
  return data
}

export async function exportForcedOrder(requestId: number): Promise<any> {
  const { data } = await api.get(`/v1/plan/forced_orders/${requestId}/export`)
  return data
}

export async function listForcedOrders(params: { limit?: number; offset?: number } = {}): Promise<any> {
  const { data } = await api.get('/v1/plan/forced_orders', { params })
  return data
}

// ===== Production control journal =====

export type ProductionControlOrderRow = {
  product_id: number
  order_id: number
  order_number: string
  order_date?: string | null
  // 'mrp' = generated by PRODPLAN, eligible for /orders/export-to-1c.
  // '1c'  = synced from 1C, already there.
  order_source?: 'mrp' | '1c' | string
  order_ref1c?: string | null
  line_number?: number | null
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  unit?: string | null
  quantity: number
  produced_qty: number
  remaining_qty: number
  status: string
  issue_status: string
  planned_start_date?: string | null
  planned_finish_date?: string | null
  opened_at?: string | null
  workshop_id?: number | null
  workshop_name?: string | null
  stage_id?: number | null
  stage_name?: string | null
  spec_id?: number | null
  issue_count: number
  route_sheet_printed_at?: string | null
  comment?: string | null
}

export async function listProductionControlOrders(params: {
  workshop_id?: number | null
  status?: string | null
  search?: string | null
  date_from?: string | null
  date_to?: string | null
  limit?: number
  offset?: number
} = {}): Promise<{ rows: ProductionControlOrderRow[]; total: number; limit: number; offset: number; latest_run_id?: number | null }> {
  const { data } = await api.get('/v1/production-control/orders', { params })
  return data
}

export async function updateProductionControlOrderState(productId: number, body: {
  status?: string
  issue_status?: string
  workshop_id?: number | null
  planned_start_date?: string | null
  planned_finish_date?: string | null
  comment?: string | null
}): Promise<any> {
  const { data } = await api.patch(`/v1/production-control/orders/${productId}/state`, body)
  return data
}

export async function getProductionControlMaterials(productId: number): Promise<any> {
  const { data } = await api.get(`/v1/production-control/orders/${productId}/materials`)
  return data
}

export async function createProductionMaterialIssues(body: {
  product_ids: number[]
  initiated_by?: string | null
  warehouse_ref1c?: string | null
}): Promise<any> {
  const { data } = await api.post('/v1/production-control/material-issues', body)
  return data
}


// ---------------------------------------------------------------------------
// Production Control settings — workshop->warehouse bindings + ignored
// warehouses. Mirrors POST/PUT/DELETE /v1/production-control/settings/*
// endpoints introduced in PR #4.
// ---------------------------------------------------------------------------

export interface WorkshopWarehouseBinding {
  binding_id: number
  workshop_id: number
  workshop_name?: string | null
  warehouse_ref1c: string
}

export interface IgnoredWarehouseEntry {
  warehouse_ref1c: string
  warehouse_name?: string | null
  reason?: string | null
}

export interface ProductionControlSettings {
  workshop_warehouse_bindings: WorkshopWarehouseBinding[]
  ignored_warehouses: IgnoredWarehouseEntry[]
}

export async function getProductionControlSettings(): Promise<ProductionControlSettings> {
  const { data } = await api.get('/v1/production-control/settings')
  return data
}

export async function upsertProductionControlWorkshopBinding(
  workshopId: number,
  warehouseRef1c: string,
): Promise<WorkshopWarehouseBinding> {
  const { data } = await api.put(
    `/v1/production-control/settings/workshop-bindings/${workshopId}`,
    { warehouse_ref1c: warehouseRef1c },
  )
  return data
}

export async function deleteProductionControlWorkshopBinding(workshopId: number): Promise<any> {
  const { data } = await api.delete(
    `/v1/production-control/settings/workshop-bindings/${workshopId}`,
  )
  return data
}

export async function upsertProductionControlIgnoredWarehouse(body: {
  warehouse_ref1c: string
  warehouse_name?: string | null
  reason?: string | null
}): Promise<IgnoredWarehouseEntry> {
  const { data } = await api.post('/v1/production-control/settings/ignored-warehouses', body)
  return data
}

export async function deleteProductionControlIgnoredWarehouse(warehouseRef1c: string): Promise<any> {
  const { data } = await api.delete(
    `/v1/production-control/settings/ignored-warehouses/${encodeURIComponent(warehouseRef1c)}`,
  )
  return data
}


// ---------------------------------------------------------------------------
// Production Control: 1C export endpoints.
// ---------------------------------------------------------------------------

export interface ExportEntry {
  order_id?: number
  issue_id?: number
  number?: string
  document_number?: string
  status: 'planned' | 'created' | 'existing' | 'error' | 'skipped' | string
  target_ref_key?: string | null
  reason?: string | null
  error?: string | null
}

export interface ExportProductionOrdersResult {
  status: 'ok' | 'partial_error' | string
  dry_run: boolean
  entity: string
  orders_requested: number
  orders_eligible: number
  orders_already_linked: number
  orders_created: number
  orders_error: number
  skipped_rows: Array<{ order_id?: number; reason?: string }>
  entries: ExportEntry[]
  payloads?: Array<{ order_id: number; number: string; payload: Record<string, any> }>
}

export async function exportProductionOrdersTo1C(body: {
  order_ids: number[]
  dry_run?: boolean
  allow_production?: boolean
}): Promise<ExportProductionOrdersResult> {
  const { data } = await api.post('/v1/production-control/orders/export-to-1c', {
    order_ids: body.order_ids,
    dry_run: body.dry_run ?? true,
    allow_production: body.allow_production ?? false,
  })
  return data
}
