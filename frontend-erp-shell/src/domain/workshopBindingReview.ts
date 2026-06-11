export type BindingReviewReason =
  | 'NO_SPEC'
  | 'NO_PRODUCTION_KIND'
  | 'KIND_NOT_BOUND'
  | 'NO_WAREHOUSE_BINDING'

export type BindingReviewItem = {
  item_id: number
  item_code: string
  item_name: string
  item_article: string
  active_lines: number
  reason_code: BindingReviewReason
  reason_text: string
  recommendation: string
  workshop_id?: number | null
  spec_id?: number | null
  spec_name?: string | null
  production_kind_id?: number | null
  production_kind_name?: string | null
  suggested_resource_id?: number | null
  suggested_resource_name?: string | null
  suggested_stage_id?: number | null
  suggested_stage_name?: string | null
}

export type BindingReviewResponse = {
  items: BindingReviewItem[]
  total: number
  limit: number
  offset: number
  scope: string
  counts_by_reason: Record<string, number>
}

export type BindingReviewLine = {
  product_id: number
  order_id: number
  order_number: string
  quantity: number
  remaining_qty: number
  status: string
  workshop_id?: number | null
  planned_start_date?: string | null
}

export type BindingReviewLinesResponse = {
  item_id: number
  rows: BindingReviewLine[]
  total: number
}

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
