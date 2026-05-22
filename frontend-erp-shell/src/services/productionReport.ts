import type { ProductionReportFactEntry, ProductionReportWeekResponse } from '../domain/productionReport'
import { api } from '../lib/api'

export function getProductionReportWeek(body: { week_start?: string; any_date_in_week?: string } = {}) {
  return api<ProductionReportWeekResponse>('/v1/plan/production_report/week', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function bulkUpsertProductionReportFact(body: { entries: ProductionReportFactEntry[]; rerun_editable_date?: string }) {
  return api<{ status: string; saved: number }>('/v1/plan/production_report/fact/bulk_upsert', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function closeProductionReportDay(body: { close_date?: string | null; closed_by?: string | null } = {}) {
  return api<Record<string, unknown>>('/v1/plan/production_report/day/close', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
