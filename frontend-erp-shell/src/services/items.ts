import type { ItemCard, ItemUpdatePayload } from '../domain/item'
import { api } from '../lib/api'

export function getItem(itemId: number) {
  return api<ItemCard>(`/v1/items/${itemId}`)
}

export function updateItem(itemId: number, payload: ItemUpdatePayload) {
  return api<ItemCard>(`/v1/items/${itemId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

// Editing a single planning attribute still needs a full-record PUT: the
// backend has no PATCH for items and `update_item()` assigns every field of
// `ItemUpdate`, whose `stock_qty` defaults to 0.0. Dropping `stock_qty` from
// the body would therefore zero the physical stock instead of leaving it
// alone, so the current value is read back and echoed unchanged.
// TODO(backend): expose a partial item update so the browser never has to
// resend `stock_qty`.
export async function updateItemOptimalBatch(itemId: number, optimalBatch: number | null) {
  const item = await getItem(itemId)
  return updateItem(itemId, {
    item_code: item.item_code,
    item_name: item.item_name,
    item_article: item.item_article ?? null,
    item_ref1c: item.item_ref1c ?? null,
    supplier_ref1c: item.supplier_ref1c ?? null,
    replenishment_time: item.replenishment_time ?? null,
    unit: item.unit ?? null,
    category_id: item.category_id ?? null,
    stock_qty: item.stock_qty,
    optimal_batch: optimalBatch,
    status: item.status,
  })
}
