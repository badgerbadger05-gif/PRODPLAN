import type { DrumResponse } from '../domain/drum'
import { api } from '../lib/api'

export function listDrum(params: { limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (typeof params.limit === 'number') search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  const query = search.toString()
  return api<DrumResponse>(`/v1/production-control/drum${query ? `?${query}` : ''}`)
}
