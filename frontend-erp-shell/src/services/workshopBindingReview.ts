import type {
  BindingReviewLinesResponse,
  BindingReviewResponse,
} from '../domain/workshopBindingReview'
import { api } from '../lib/api'

export function listReviewItems(params: {
  scope: 'active' | 'catalog'
  search?: string
  reasonCode?: string
  limit?: number
  offset?: number
}) {
  const query = new URLSearchParams()
  query.set('scope', params.scope)
  if (params.search) query.set('search', params.search)
  if (params.reasonCode) query.set('reason_code', params.reasonCode)
  query.set('limit', String(params.limit ?? 100))
  query.set('offset', String(params.offset ?? 0))
  return api<BindingReviewResponse>(`/v1/workshop-binding-review/items?${query.toString()}`)
}

export function listReviewItemLines(itemId: number) {
  return api<BindingReviewLinesResponse>(`/v1/workshop-binding-review/items/${itemId}/lines`)
}

export function assignLineWorkshop(productId: number, workshopId: number) {
  return api<Record<string, unknown>>(`/v1/production-control/orders/${productId}/state`, {
    method: 'PATCH',
    body: JSON.stringify({ workshop_id: workshopId }),
  })
}
