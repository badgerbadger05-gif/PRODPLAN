import type {
  ControlSettings,
  EmployeesResponse,
  MaterialIssueDetail,
  MaterialIssueCreateResponse,
  MaterialIssueCreatePayload,
  MaterialsResponse,
  OrdersResponse,
  ProductionOperationsResponse,
  ProduceLinePayload,
  ProduceLineResult,
  TransferIssuesResponse,
  ReturnLeftoversResult,
} from '../domain/productionControl'
import { api, apiText } from '../lib/api'
import type { components } from '../lib/apiTypes'

type ApiSchemas = components['schemas']

export type ControlSettingsUpdate = {
  workshop_warehouses: Array<{
    resource_id?: number
    workshop_id?: number
    warehouse_ref1c: string
    production_warehouse_ref1c: string
  }>
  ignored_warehouses: Array<{ warehouse_ref1c: string }>
}

export type OrderQuantityUpdateResult = {
  quantity: number
  remaining_qty: number
  mrp_req_net_qty?: number | null
  mrp_req_covered_qty?: number | null
  mrp_req_remaining_qty?: number | null
}

// Loosely-typed side of the paint↔weld chain preview/close response. The
// endpoint returns a heterogeneous document the page reads field-by-field.
export function listProductionOrders(params: URLSearchParams) {
  return api<OrdersResponse>(`/v1/production-control/orders?${params.toString()}`)
}

export function listProductionEmployees() {
  return api<EmployeesResponse>('/v1/production-control/employees')
}

export function listProductionOperations(productId: number) {
  return api<ProductionOperationsResponse>(`/v1/production-control/orders/${productId}/operations`)
}

export function getProductionControlSettings() {
  return api<ControlSettings>('/v1/production-control/settings')
}

export function saveProductionControlSettings(payload: ControlSettingsUpdate) {
  const request: ApiSchemas['SettingsPayload'] = payload
  return api<ControlSettings>('/v1/production-control/settings', {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export function getOrderMaterials(productId: number) {
  return api<MaterialsResponse>(`/v1/production-control/orders/${productId}/materials`)
}

export function refreshOrderMaterials(productId: number) {
  return api<MaterialsResponse>(`/v1/production-control/orders/${productId}/materials/refresh`, {
    method: 'POST',
  })
}

export function updateOrderStatus(productId: number, status: string) {
  return api(`/v1/production-control/orders/${productId}/state`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })
}

export function postMaterialIssues(productIds: number[], initiatedBy: string, sourceWarehouseRef?: string) {
  const body: ApiSchemas['MaterialIssueCreatePayload'] = {
    product_ids: productIds,
    initiated_by: initiatedBy,
    ...(sourceWarehouseRef ? { source_warehouse_ref1c: sourceWarehouseRef } : {}),
  }
  return api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function fetchRouteSheetsPrintHtml(productIds: number[]): Promise<string> {
  return apiText('/v1/production-control/route-sheets/print', {
    method: 'POST',
    body: JSON.stringify({ product_ids: productIds, mark_printed: true, auto_print: true }),
  })
}

export function exportMaterialIssuesTo1C(issueIds: number[]) {
  const payload: ApiSchemas['ExportMaterialIssuesPayload'] = {
    issue_ids: issueIds,
    dry_run: false,
    allow_production: true,
  }
  return api<Record<string, unknown>>('/v1/production-control/material-issues/export-to-1c', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function markMaterialIssueAssembled(issueId: number) {
  return api(`/v1/production-control/material-issues/${issueId}/assembled`, {
    method: 'POST',
    body: JSON.stringify({ allow_production: true }),
  })
}

export function listMaterialIssues(params: URLSearchParams) {
  return api<TransferIssuesResponse>(`/v1/production-control/material-issues?${params.toString()}`)
}

export function getMaterialIssue(issueId: number) {
  return api<MaterialIssueDetail>(`/v1/production-control/material-issues/${issueId}`)
}

export function deleteMaterialIssue(issueId: number) {
  return api(`/v1/production-control/material-issues/${issueId}`, { method: 'DELETE' })
}

export function syncPostedTransfers() {
  return api<Record<string, unknown>>('/v1/production-control/sync-posted-transfers', {
    method: 'POST',
  })
}

export function updateOrderQuantity(productId: number, quantity: number) {
  const payload: ApiSchemas['UpdateQuantityPayload'] = { quantity }
  return api<OrderQuantityUpdateResult>(`/v1/production-control/orders/${productId}/quantity`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function produceOrderLine(productId: number, payload: ProduceLinePayload) {
  return api<ProduceLineResult>(`/v1/production-control/orders/${productId}/produce`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function returnLeftoverComponents(productId: number, initiatedBy?: string | null) {
  const query = initiatedBy ? `?initiated_by=${encodeURIComponent(initiatedBy)}` : ''
  return api<ReturnLeftoversResult>(`/v1/production-control/orders/${productId}/return-leftovers${query}`, {
    method: 'POST',
  })
}

export function getItem(itemId: number) {
  return api<Record<string, unknown>>(`/v1/items/${itemId}`)
}

export function updateItem(itemId: number, payload: Record<string, unknown>) {
  return api(`/v1/items/${itemId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function createMaterialIssues(payload: MaterialIssueCreatePayload) {
  return api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function deleteProductionOrder(productId: number) {
  return api(`/v1/production-control/orders/${productId}`, { method: 'DELETE' })
}
