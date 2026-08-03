import { api } from '../lib/api'
import type { components } from '../lib/apiTypes'

type ApiSchemas = components['schemas']

// Legacy plan-matrix endpoints (/plan/matrix, /plan/bulk_upsert, /plan/delete_row,
// /plan/ensure_item) were removed from the backend by owner decision. The period-plan
// workflow now lives in services/periodPlan.ts. Only nomenclature search remains here.
export function searchNomenclature(query: string) {
  const search = new URLSearchParams()
  search.set('q', query)
  search.set('limit', '12')
  return api<ApiSchemas['NomenclatureSearchResponse']>(`/v1/nomenclature/search?${search.toString()}`)
}
