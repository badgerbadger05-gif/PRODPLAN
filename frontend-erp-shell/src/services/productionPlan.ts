import type { NomenclatureSearchItem } from '../domain/productionPlan'
import { api } from '../lib/api'

// Legacy plan-matrix endpoints (/plan/matrix, /plan/bulk_upsert, /plan/delete_row,
// /plan/ensure_item) were removed from the backend by owner decision. The period-plan
// workflow now lives in services/periodPlan.ts. Only nomenclature search remains here.
export function searchNomenclature(query: string) {
  const search = new URLSearchParams()
  search.set('q', query)
  search.set('limit', '12')
  return api<{ items: NomenclatureSearchItem[]; total: number; query: string; search_type: string }>(`/v1/nomenclature/search?${search.toString()}`)
}
