import type { components } from '../lib/apiTypes'

type ApiSchemas = components['schemas']

export type BindingReviewItem = ApiSchemas['BindingReviewItemResponse']
export type BindingReviewReason = BindingReviewItem['reason_code']
export type BindingReviewResponse = ApiSchemas['BindingReviewItemsResponse']
export type BindingReviewLine = ApiSchemas['BindingReviewLineResponse']
export type BindingReviewLinesResponse = ApiSchemas['BindingReviewLinesResponse']

export const reasonLabels: Record<BindingReviewReason, string> = {
  NO_SPEC: 'Нет спецификации',
  NO_PRODUCTION_KIND: 'Не заполнен вид производства',
  KIND_NOT_BOUND: 'Вид не привязан к участку',
  NO_WAREHOUSE_BINDING: 'Не настроен склад участка',
}

// pill colour classes reused from the journal statuses
export const reasonPillClass: Record<BindingReviewReason, string> = {
  NO_SPEC: 'shortage',
  NO_PRODUCTION_KIND: 'shortage',
  KIND_NOT_BOUND: 'to_move',
  NO_WAREHOUSE_BINDING: 'to_move',
}
