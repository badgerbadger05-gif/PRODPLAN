import type { AssemblyQueueResponse } from '../domain/assemblyQueue'
import { api } from '../lib/api'

export function listAssemblyQueue(params: { limit?: number; offset?: number } = {}) {
  const search = new URLSearchParams()
  if (typeof params.limit === 'number') search.set('limit', String(params.limit))
  if (typeof params.offset === 'number') search.set('offset', String(params.offset))
  const query = search.toString()
  return api<AssemblyQueueResponse>(
    `/v1/production-control/assembly-queue${query ? `?${query}` : ''}`,
  )
}
