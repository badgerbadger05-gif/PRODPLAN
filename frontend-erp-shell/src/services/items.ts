import type { ItemCard, ItemPatchPayload, ItemUpdatePayload } from '../domain/item'
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

export function patchItem(itemId: number, payload: ItemPatchPayload) {
  return api<ItemCard>(`/v1/items/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

// Editing one planning attribute sends exactly that attribute: the physical
// stock is never read back and echoed, so a concurrent 1C sync cannot be
// overwritten with a stale copy.
export function updateItemOptimalBatch(itemId: number, optimalBatch: number | null) {
  return patchItem(itemId, { optimal_batch: optimalBatch })
}
