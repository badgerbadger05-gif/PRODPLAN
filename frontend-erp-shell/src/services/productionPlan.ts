import type { NomenclatureSearchItem, PlanChange, PlanMatrixResponse } from '../domain/productionPlan'
import { api } from '../lib/api'

export function getPlanMatrix(body: {
  start_date: string
  days: number
  page?: number
  page_size?: number
  sort_by?: string
  sort_dir?: string
}) {
  return api<PlanMatrixResponse>('/v1/plan/matrix', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function bulkUpsertPlan(entries: PlanChange[]) {
  return api<{ status: string; saved: number }>('/v1/plan/bulk_upsert', {
    method: 'POST',
    body: JSON.stringify({ entries }),
  })
}

export function searchNomenclature(query: string) {
  const search = new URLSearchParams()
  search.set('q', query)
  search.set('limit', '12')
  return api<{ items: NomenclatureSearchItem[]; total: number; query: string; search_type: string }>(`/v1/nomenclature/search?${search.toString()}`)
}

export function ensurePlanItem(item: NomenclatureSearchItem) {
  return api<{ status: string; item_id: number }>('/v1/plan/ensure_item', {
    method: 'POST',
    body: JSON.stringify({
      item_code: item.item_code,
      item_name: item.item_name,
      item_article: item.item_article ?? null,
    }),
  })
}

export function deletePlanRow(body: { item_id: number; start_date: string; days: number }) {
  return api<{ status: string; deleted: number; root_deleted: number }>('/v1/plan/delete_row', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
