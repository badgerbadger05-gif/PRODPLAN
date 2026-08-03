import type {
  MrpPagedResponse,
  MrpCapacityRow,
  MrpProductionRow,
  MrpPurchaseRow,
  MrpReworkRow,
  MrpSummary,
  PlanningRunsResponse,
} from '../domain/planning'
import { api } from '../lib/api'

export function listPlanningRuns(params: { limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (params.limit) search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  return api<PlanningRunsResponse>(`/v1/plan/runs?${search.toString()}`)
}

export function getPlanningRunSummary(runId: number) {
  return api<MrpSummary>(`/v1/plan/results/${runId}`)
}

type MrpSnapshotQuery = {
  snapshot_id: number
  format?: string
  date_from?: string
  date_to?: string
  root_item_id?: number | null
  supplier_ref1c?: string | null
  category_id?: number | null
  category_ref1c?: string | null
  limit?: number
  offset?: number
}

function buildResultQuery(params: MrpSnapshotQuery) {
  const search = new URLSearchParams()
  search.set('snapshot_id', String(params.snapshot_id))
  if (params.format) search.set('format', params.format)
  if (params.date_from) search.set('date_from', params.date_from)
  if (params.date_to) search.set('date_to', params.date_to)
  if (params.root_item_id) search.set('root_item_id', String(params.root_item_id))
  if (params.supplier_ref1c) search.set('supplier_ref1c', params.supplier_ref1c)
  if (params.category_id) search.set('category_id', String(params.category_id))
  if (params.category_ref1c) search.set('category_ref1c', params.category_ref1c)
  if (params.limit) search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  return search.toString()
}

export function getPlanningResultProduction(runId: number, params: MrpSnapshotQuery) {
  return api<MrpPagedResponse<MrpProductionRow>>(`/v1/plan/results/${runId}/production?${buildResultQuery(params)}`)
}

export function getPlanningResultPurchases(runId: number, params: MrpSnapshotQuery) {
  return api<MrpPagedResponse<MrpPurchaseRow>>(`/v1/plan/results/${runId}/purchases?${buildResultQuery(params)}`)
}

export function getPlanningResultRework(runId: number, params: MrpSnapshotQuery) {
  return api<MrpPagedResponse<MrpReworkRow>>(`/v1/plan/results/${runId}/rework?${buildResultQuery(params)}`)
}

export function getPlanningResultCapacity(runId: number, params: MrpSnapshotQuery) {
  return api<MrpPagedResponse<MrpCapacityRow>>(`/v1/plan/results/${runId}/capacity?${buildResultQuery(params)}`)
}

export function exportPlanningResultProduction(runId: number, params: MrpSnapshotQuery & { format: 'csv' | 'xlsx' }) {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>(
    `/v1/plan/results/${runId}/production/export?${buildResultQuery(params)}`,
  )
}

export function exportPlanningResultPurchases(runId: number, params: MrpSnapshotQuery & { format: 'csv' | 'xlsx' }) {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>(
    `/v1/plan/results/${runId}/purchases/export?${buildResultQuery(params)}`,
  )
}

export function exportPlanningResultRework(runId: number, params: MrpSnapshotQuery & { format: 'csv' | 'xlsx' }) {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>(
    `/v1/plan/results/${runId}/rework/export?${buildResultQuery(params)}`,
  )
}

export function exportPurchasesTo1C(runId: number, body: {
  date_from?: string
  date_to?: string
  purchase_ids?: number[]
  dry_run?: boolean
  allow_production?: boolean
}) {
  return api<Record<string, unknown>>(`/v1/plan/results/${runId}/purchases/export-to-1c`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
