import type { ShelvesResponse } from '../domain/shelves'
import { api } from '../lib/api'

export function listShelves(params: { limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (typeof params.limit === 'number') search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  const query = search.toString()
  return api<ShelvesResponse>(`/v1/production-control/shelves${query ? `?${query}` : ''}`)
}
