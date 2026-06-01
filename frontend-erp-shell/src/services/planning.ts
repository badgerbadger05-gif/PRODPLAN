import type {
  MrpPagedResponse,
  MrpCapacityRow,
  MrpProductionRow,
  MrpPurchaseRow,
  MrpReworkRow,
  MrpSummary,
  PlanningRunsResponse,
  StartPlanningRunResponse,
} from '../domain/planning'
import { api } from '../lib/api'

export function listPlanningRuns(params: { limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (params.limit) search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  return api<PlanningRunsResponse>(`/v1/plan/runs?${search.toString()}`)
}

export function startPlanningRun(body: { horizon_days?: number; started_by?: string } = {}) {
  return api<StartPlanningRunResponse>('/v1/plan/calc', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function getPlanningRunSummary(runId: number) {
  return api<MrpSummary>(`/v1/plan/results/${runId}`)
}

function buildResultQuery(params: { format?: string; date_from?: string; date_to?: string; root_item_id?: number | null; limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (params.format) search.set('format', params.format)
  if (params.date_from) search.set('date_from', params.date_from)
  if (params.date_to) search.set('date_to', params.date_to)
  if (params.root_item_id) search.set('root_item_id', String(params.root_item_id))
  if (params.limit) search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  return search.toString()
}

export function getPlanningResultProduction(runId: number, params: { date_from?: string; date_to?: string; root_item_id?: number | null; limit?: number; offset?: number } = {}) {
  return api<MrpPagedResponse<MrpProductionRow>>(`/v1/plan/results/${runId}/production?${buildResultQuery(params)}`)
}

export function getPlanningResultPurchases(runId: number, params: { date_from?: string; date_to?: string; root_item_id?: number | null; limit?: number; offset?: number } = {}) {
  return api<MrpPagedResponse<MrpPurchaseRow>>(`/v1/plan/results/${runId}/purchases?${buildResultQuery(params)}`)
}

export function getPlanningResultRework(runId: number, params: { date_from?: string; date_to?: string; root_item_id?: number | null; limit?: number; offset?: number } = {}) {
  return api<MrpPagedResponse<MrpReworkRow>>(`/v1/plan/results/${runId}/rework?${buildResultQuery(params)}`)
}

export function getPlanningResultCapacity(runId: number, params: { date_from?: string; date_to?: string; limit?: number; offset?: number } = {}) {
  return api<MrpPagedResponse<MrpCapacityRow>>(`/v1/plan/results/${runId}/capacity?${buildResultQuery(params)}`)
}

export function exportPlanningResultProduction(runId: number, params: { format: 'csv' | 'xlsx'; date_from?: string; date_to?: string; root_item_id?: number | null }) {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>(
    `/v1/plan/results/${runId}/production/export?${buildResultQuery(params)}`,
  )
}

export function exportPlanningResultPurchases(runId: number, params: { format: 'csv' | 'xlsx'; date_from?: string; date_to?: string; root_item_id?: number | null }) {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>(
    `/v1/plan/results/${runId}/purchases/export?${buildResultQuery(params)}`,
  )
}

export function exportPlanningResultRework(runId: number, params: { format: 'csv' | 'xlsx'; date_from?: string; date_to?: string; root_item_id?: number | null }) {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>(
    `/v1/plan/results/${runId}/rework/export?${buildResultQuery(params)}`,
  )
}

export function getShortageReport(runId: number) {
  return api<{ data_base64?: string; filename?: string; content_type?: string; message?: string }>(
    `/v1/plan/results/${runId}/shortage-report`,
  )
}

export function createProductionControlOrdersFromMrp(body: {
  run_id: number
  date_from?: string
  date_to?: string
  planned_order_ids?: number[]
  dry_run?: boolean
}) {
  return api<Record<string, unknown>>('/v1/production-control/orders/from-mrp', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function exportPurchasesTo1C(runId: number, body: {
  date_from?: string
  date_to?: string
  purchase_ids?: number[]
  dry_run?: boolean
}) {
  return api<Record<string, unknown>>(`/v1/plan/results/${runId}/purchases/export-to-1c`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
