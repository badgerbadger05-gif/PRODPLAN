import type { PurchaseFiltersResponse, PurchaseOrderCard, PurchaseOrdersResponse } from '../domain/purchaseControl'
import { syncActions } from '../domain/sync'
import { api } from '../lib/api'
import { getODataConfig } from './sync'

export function listPurchaseJournal(params: URLSearchParams) {
  return api<PurchaseOrdersResponse>(`/v1/purchase-control/orders?${params.toString()}`)
}

export function getPurchaseOrderCard(orderId: number) {
  return api<PurchaseOrderCard>(`/v1/purchase-control/orders/${orderId}`)
}

export function getPurchaseFilters() {
  return api<PurchaseFiltersResponse>('/v1/purchase-control/filters')
}

export function materializePurchaseControlRows(payload: {
  snapshot_id: number
  row_keys: string[]
  dry_run?: boolean
}) {
  return api<Record<string, unknown>>('/v1/purchase-control/materialize', {
    method: 'POST',
    body: JSON.stringify({
      snapshot_id: payload.snapshot_id,
      row_keys: payload.row_keys,
      dry_run: payload.dry_run ?? false,
    }),
  })
}

export async function syncSupplierOrdersFrom1C() {
  const action = syncActions.find((row) => row.id === 'supplierOrders')
  if (!action) throw new Error('Не найдено действие синхронизации заказов поставщику')
  const config = await getODataConfig()
  if (!config?.base_url) throw new Error('Не настроено подключение OData (страница «Синхронизация»)')
  return api<Record<string, unknown>>(action.endpoint, {
    method: 'POST',
    body: JSON.stringify({ ...config, entity_name: action.entity_name, dry_run: false, zero_missing: false }),
  })
}
