import type { FeasibilityResponse, FeasibilitySearchResponse } from '../domain/releaseFeasibility'
import { api } from '../lib/api'

export function searchReleaseItems(params: { q: string; limit?: number }) {
  const search = new URLSearchParams()
  search.set('q', params.q)
  search.set('limit', String(params.limit ?? 50))
  return api<FeasibilitySearchResponse>(`/v1/release-feasibility/search?${search.toString()}`)
}

export function analyzeRelease(params: {
  item_id?: number
  article?: string
  qty: number
  max_depth?: number
  include_tree?: boolean
}) {
  const search = new URLSearchParams()
  if (params.item_id != null) search.set('item_id', String(params.item_id))
  if (params.article) search.set('article', params.article)
  search.set('qty', String(params.qty))
  if (params.max_depth != null) search.set('max_depth', String(params.max_depth))
  if (params.include_tree) search.set('include_tree', 'true')
  return api<FeasibilityResponse>(`/v1/release-feasibility/analyze?${search.toString()}`)
}
