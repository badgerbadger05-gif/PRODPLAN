// Item card as served by /v1/items/{id} (backend schemas.Item). The write DTO
// (schemas.ItemUpdate) carries exactly the ItemBase fields — there is no
// partial update endpoint, so PUT always replaces the whole record.
export type ItemCard = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  item_ref1c?: string | null
  supplier_ref1c?: string | null
  replenishment_time?: number | null
  unit?: string | null
  category_id?: number | null
  stock_qty: number
  optimal_batch?: number | null
  status: string
  created_at?: string | null
  updated_at?: string | null
}

export type ItemUpdatePayload = {
  item_code: string
  item_name: string
  item_article?: string | null
  item_ref1c?: string | null
  supplier_ref1c?: string | null
  replenishment_time?: number | null
  unit?: string | null
  category_id?: number | null
  stock_qty: number
  optimal_batch?: number | null
  status: string
}
