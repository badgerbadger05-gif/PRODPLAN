import type { ShelvesResponse } from '../domain/shelves'
import { api } from '../lib/api'

export function listShelves() {
  return api<ShelvesResponse>('/v1/production-control/shelves')
}
