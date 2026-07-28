import type {
  ControlSettings,
  MaterialIssueCreatePayload,
  MaterialIssueCreateResponse,
  MaterialsResponse,
  OrderQuantityPatchResponse,
  OrderStatePatchPayload,
  OrdersResponse,
  ProduceLinePayload,
  ProduceLineResult,
  ReturnLeftoversResult,
  SyncPostedTransfersResponse,
  TransferIssueRow,
  ProductionFilters,
} from '../domain/productionControl'
import { api, apiText } from '../lib/api'
import { listResources } from './resources'

export function listProductionOrders(params: {
  focusProductId?: string | null
  focusOrderId?: string | null
  search?: string
  status?: string
  workshop_id?: string
  coverage_status?: string
  root_item_id?: string
  sort_by?: ProductionFilters['sort_by']
  sort_dir?: ProductionFilters['sort_dir']
  limit?: number
  offset?: number
}) {
  const query = new URLSearchParams()
  query.set('limit', String(params.limit ?? 100))
  query.set('offset', String(params.offset ?? 0))
  if (params.focusProductId) query.set('product_id', params.focusProductId)
  if (params.focusOrderId) query.set('order_id', params.focusOrderId)
  if (params.search) query.set('search', params.search)
  if (params.status) query.set('status', params.status)
  if (params.workshop_id) query.set('workshop_id', params.workshop_id)
  if (params.coverage_status) query.set('coverage_status', params.coverage_status)
  if (params.root_item_id) query.set('root_item_id', params.root_item_id)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_dir) query.set('sort_dir', params.sort_dir)

  return api<OrdersResponse>(`/v1/production-control/orders?${query.toString()}`)
}

export function listProductionResources() {
  return listResources()
}

export function getOrderMaterials(productId: number) {
  return api<MaterialsResponse>(`/v1/production-control/orders/${productId}/materials`)
}

export function refreshOrderMaterials(productId: number) {
  return api<MaterialsResponse>(`/v1/production-control/orders/${productId}/materials/refresh`, {
    method: 'POST',
  })
}

export function updateOrderQuantity(productId: number, quantity: number) {
  return api<OrderQuantityPatchResponse>(`/v1/production-control/orders/${productId}/quantity`, {
    method: 'PATCH',
    body: JSON.stringify({ quantity }),
  })
}

export function updateOrderState(productId: number, payload: OrderStatePatchPayload) {
  return api<Record<string, unknown>>(`/v1/production-control/orders/${productId}/state`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function createMaterialIssues(body: MaterialIssueCreatePayload) {
  return api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function exportMaterialIssuesTo1C(
  issueIds: number[],
  options: { dry_run?: boolean; allow_production?: boolean } = {},
) {
  return api<Record<string, unknown>>('/v1/production-control/material-issues/export-to-1c', {
    method: 'POST',
    body: JSON.stringify({
      issue_ids: issueIds,
      dry_run: options.dry_run ?? false,
      allow_production: options.allow_production ?? true,
    }),
  })
}

export function syncPostedTransfers() {
  return api<SyncPostedTransfersResponse>('/v1/production-control/sync-posted-transfers', {
    method: 'POST',
  })
}

export function produceOrderLine(productId: number, payload: ProduceLinePayload) {
  return api<ProduceLineResult>(`/v1/production-control/orders/${productId}/produce`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function closePaintWeldChain(productId: number) {
  return api<Record<string, unknown>>('/v1/paint-weld/chain/close', {
    method: 'POST',
    body: JSON.stringify({
      product_id: productId,
      dry_run: false,
      allow_production: true,
      initiated_by: 'erp-shell-chain-close',
    }),
  })
}

export function returnLeftoverComponents(productId: number, initiatedBy?: string | null) {
  const query = initiatedBy ? `?initiated_by=${encodeURIComponent(initiatedBy)}` : ''
  return api<ReturnLeftoversResult>(
    `/v1/production-control/orders/${productId}/return-leftovers${query}`,
    { method: 'POST' },
  )
}

export function openMaterialIssue(issueId: number) {
  return api<TransferIssueRow>(`/v1/production-control/material-issues/${issueId}`)
}

export function cancelLocalProductionOrder(productId: number) {
  return api<Record<string, unknown>>(`/v1/production-control/orders/${productId}`, {
    method: 'DELETE',
  })
}

export function getProductionSettings() {
  return api<ControlSettings>('/v1/production-control/settings')
}

export function saveProductionSettings(payload: {
  warehouses: ControlSettings['warehouses']
  workshop_warehouses: ControlSettings['workshop_warehouses']
  ignored_warehouses: ControlSettings['ignored_warehouses']
}) {
  return api<ControlSettings>('/v1/production-control/settings', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

// The backend answers with `HTMLResponse`, so the raw document text is read
// through `apiText` — `api()` would try to parse it as JSON and always fail.
export function printRouteSheets(productIds: number[], options: { markPrinted?: boolean; autoPrint?: boolean } = {}) {
  return apiText('/v1/production-control/route-sheets/print', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      product_ids: productIds,
      mark_printed: options.markPrinted ?? true,
      auto_print: options.autoPrint ?? true,
    }),
  })
}
