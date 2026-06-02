import type {
  ExecutionJournalResponse,
  PeriodPlan,
  PeriodPlanListResponse,
  PeriodPlanMatrix,
  PeriodPlanRun,
  PeriodPlanRunsResponse,
} from '../domain/planning'
import { api } from '../lib/api'

export function listPeriodPlans(params: {
  status?: string
  period_from?: string
  period_to?: string
  created_by?: string
  sort_by?: string
  sort_dir?: 'asc' | 'desc'
  limit?: number
  offset?: number
} = {}) {
  const search = new URLSearchParams()
  if (params.status) search.set('status', params.status)
  if (params.period_from) search.set('period_from', params.period_from)
  if (params.period_to) search.set('period_to', params.period_to)
  if (params.created_by) search.set('created_by', params.created_by)
  if (params.sort_by) search.set('sort_by', params.sort_by)
  if (params.sort_dir) search.set('sort_dir', params.sort_dir)
  if (params.limit) search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  return api<PeriodPlanListResponse>(`/v1/plan/period-plans?${search.toString()}`)
}

export function createPeriodPlan(body: {
  name: string
  period_from: string
  period_to: string
  comment?: string | null
  created_by?: string | null
}) {
  return api<PeriodPlan>('/v1/plan/period-plans', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function updatePeriodPlanHeader(
  planId: number,
  body: { name?: string; period_from?: string; period_to?: string; comment?: string | null },
) {
  return api<PeriodPlan>(`/v1/plan/period-plans/${planId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function archivePeriodPlan(planId: number) {
  return api<PeriodPlan>(`/v1/plan/period-plans/${planId}/archive`, { method: 'POST' })
}

export function unarchivePeriodPlan(planId: number) {
  return api<PeriodPlan>(`/v1/plan/period-plans/${planId}/unarchive`, { method: 'POST' })
}

export function listPeriodPlanRuns(planId: number, limit = 50) {
  return api<PeriodPlanRunsResponse>(`/v1/plan/period-plans/${planId}/runs?limit=${limit}`)
}

// Re-export for callers that want the row type
export type { PeriodPlanRun }

export function fixPeriodPlan(planId: number, fixedBy = 'erp-shell') {
  return api<PeriodPlan>(`/v1/plan/period-plans/${planId}/fix`, {
    method: 'POST',
    body: JSON.stringify({ fixed_by: fixedBy }),
  })
}

export function createMrpSnapshot(planId: number) {
  return api<{ status: string; run_id: number; plan_id: number; requirement_count: number; purchase_count: number; rework_count: number }>(
    `/v1/plan/period-plans/${planId}/mrp-snapshot`,
    { method: 'POST', body: JSON.stringify({ started_by: 'erp-shell' }) },
  )
}

export function getPeriodPlanMatrix(planId: number) {
  return api<PeriodPlanMatrix>(`/v1/plan/period-plans/${planId}/matrix`)
}

export function bulkUpsertPeriodPlanLines(planId: number, entries: Array<{ item_id: number; bucket_date: string; qty: number }>) {
  return api<{ status: string; saved: number }>(`/v1/plan/period-plans/${planId}/lines/bulk_upsert`, {
    method: 'POST',
    body: JSON.stringify({ entries }),
  })
}

export function getExecutionJournal(planId: number, params: { run_id?: number; root_item_id?: number | null; bom_level?: number; flow?: string } = {}) {
  const search = new URLSearchParams()
  if (params.run_id) search.set('run_id', String(params.run_id))
  if (params.root_item_id) search.set('root_item_id', String(params.root_item_id))
  if (typeof params.bom_level === 'number') search.set('bom_level', String(params.bom_level))
  if (params.flow) search.set('flow', params.flow)
  return api<ExecutionJournalResponse>(`/v1/plan/period-plans/${planId}/execution-journal?${search.toString()}`)
}

export function deletePeriodPlan(planId: number) {
  return api<{ status: string; id: number; name: string }>(`/v1/plan/period-plans/${planId}`, {
    method: 'DELETE',
  })
}

export function addItemToPeriodPlan(planId: number, itemId: number) {
  return api<{ status: string; plan_id: number; item_id: number }>(`/v1/plan/period-plans/${planId}/items`, {
    method: 'POST',
    body: JSON.stringify({ item_id: itemId }),
  })
}

export function deleteItemFromPeriodPlan(planId: number, itemId: number) {
  return api<{ status: string; plan_id: number; item_id: number; deleted: number }>(
    `/v1/plan/period-plans/${planId}/items/${itemId}`,
    { method: 'DELETE' },
  )
}

export type ReconcileResult = {
  status: string
  run_id: number
  production_added: { item_id: number; item_code?: string; qty: number }[]
  purchase_added: { item_id: number; item_code?: string; qty: number }[]
  rescheduled?: { floating: number; fixed: number; warnings: unknown[] }
}

// Пересчёт остаточной потребности по снимку: добор недопокрытия (заказы в
// журнал, строки закупок) + перепланировка ещё не открытых в 1С заказов от
// сегодня. В 1С ничего не пишется — только по кнопке пользователя.
export function reconcileRun(runId: number) {
  return api<ReconcileResult>(`/v1/plan/results/${runId}/reconcile`, {
    method: 'POST',
    body: JSON.stringify({ dry_run: false }),
  })
}

export function createProductionOrdersFromRequirements(requirementIds: number[], initiatedBy = 'erp-shell') {
  return api<{ status: string; created: unknown[]; reused: unknown[]; skipped: unknown[]; errors: string[] }>(
    '/v1/production-control/orders/from-mrp-requirements',
    {
      method: 'POST',
      body: JSON.stringify({ requirement_ids: requirementIds, initiated_by: initiatedBy }),
    },
  )
}
