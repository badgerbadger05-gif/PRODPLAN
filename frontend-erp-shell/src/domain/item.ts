// Item card as served by /v1/items/{id} (backend schemas.Item).
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

// Full-record write DTO of PUT /v1/items/{id} (backend schemas.ItemUpdate):
// every field is applied, so an omitted one falls back to its schema default.
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

// Partial write DTO of PATCH /v1/items/{id} (backend schemas.ItemPatch): only
// the sent keys are written. `stock_qty` is absent on purpose — physical stock
// belongs to the 1C sync and the Item Ledger, and the endpoint answers 422 to
// any attempt to carry it.
export type ItemPatchPayload = {
  item_code?: string
  item_name?: string
  item_article?: string | null
  item_ref1c?: string | null
  supplier_ref1c?: string | null
  replenishment_time?: number | null
  unit?: string | null
  category_id?: number | null
  optimal_batch?: number | null
  status?: string
}
