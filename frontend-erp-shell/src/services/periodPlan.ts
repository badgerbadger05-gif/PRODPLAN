import type {
  ExecutionJournalResponse,
  PeriodPlan,
  PeriodPlanListResponse,
  PeriodPlanMatrix,
} from '../domain/planning'
import { api } from '../lib/api'

export function listPeriodPlans(params: { status?: string; limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (params.status) search.set('status', params.status)
  if (params.limit) search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  return api<PeriodPlanListResponse>(`/v1/plan/period-plans?${search.toString()}`)
}

export function createPeriodPlan(body: { name: string; period_from: string; period_to: string }) {
  return api<PeriodPlan>('/v1/plan/period-plans', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

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

export function getExecutionJournal(planId: number, params: { run_id?: number; bom_level?: number; flow?: string } = {}) {
  const search = new URLSearchParams()
  if (params.run_id) search.set('run_id', String(params.run_id))
  if (typeof params.bom_level === 'number') search.set('bom_level', String(params.bom_level))
  if (params.flow) search.set('flow', params.flow)
  return api<ExecutionJournalResponse>(`/v1/plan/period-plans/${planId}/execution-journal?${search.toString()}`)
}

export function allocatePurchases(runId: number) {
  return api<{ status: string; updated_count: number }>(`/v1/plan/results/${runId}/purchases/allocate`, { method: 'POST' })
}

export function allocateRework(runId: number) {
  return api<{ status: string; updated_count: number }>(`/v1/plan/results/${runId}/rework/allocate`, { method: 'POST' })
}
