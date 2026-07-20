import type {
  ControlSettings,
  EmployeesResponse,
  MaterialIssueCreateResponse,
  MaterialsResponse,
  OrdersResponse,
  ProductionOperationsResponse,
} from '../domain/productionControl'
import { api } from '../lib/api'

// Executor row shared by the produce dialog and the paint↔weld chain close.
export type OperationExecutorInput = {
  line_number: number
  spec_operation_id: number
  operation_id: number
  employee_ref1c: string
}

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

export type ProduceOrderRequest = {
  qty: number
  executor?: string
  operation_executors?: OperationExecutorInput[]
}

// Loosely-typed side of the paint↔weld chain preview/close response. The
// endpoint returns a heterogeneous document the page reads field-by-field.
export type PaintWeldChainSide = {
  product_id?: number
  qty_to_produce?: number
  existing_manufacture_id?: number | null
  [key: string]: unknown
}

export type PaintWeldChainResult = {
  status?: string
  weld?: PaintWeldChainSide
  paint?: PaintWeldChainSide
  piecework_export?: Record<string, unknown>
  manufactures_export?: Record<string, unknown>
  [key: string]: unknown
}

export type PaintWeldChainCloseRequest = {
  product_id: number
  dry_run: boolean
  allow_production?: boolean
  weld_operation_executors?: OperationExecutorInput[]
  paint_operation_executors?: OperationExecutorInput[]
  initiated_by?: string
}

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
  return api<ControlSettings>('/v1/production-control/settings', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getOrderMaterials(productId: number, refresh: boolean) {
  return api<MaterialsResponse>(
    `/v1/production-control/orders/${productId}/materials${refresh ? '?refresh=true' : ''}`,
  )
}

export function updateOrderStatus(productId: number, status: string) {
  return api(`/v1/production-control/orders/${productId}/state`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })
}

export function postMaterialIssues(productIds: number[], initiatedBy: string, sourceWarehouseRef?: string) {
  const body: Record<string, unknown> = { product_ids: productIds, initiated_by: initiatedBy }
  if (sourceWarehouseRef) body.source_warehouse_ref1c = sourceWarehouseRef
  return api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// Route-sheet print returns HTML (not JSON), so it bypasses the api() wrapper
// and stays a raw fetch. Resolves with the HTML body; throws on a non-2xx.
export async function fetchRouteSheetsPrintHtml(productIds: number[]): Promise<string> {
  const response = await fetch('/api/v1/production-control/route-sheets/print', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ product_ids: productIds, mark_printed: true, auto_print: true }),
  })
  const html = await response.text()
  if (!response.ok) throw new Error(html || response.statusText)
  return html
}

export function exportMaterialIssuesTo1C(issueIds: number[]) {
  return api<Record<string, unknown>>('/v1/production-control/material-issues/export-to-1c', {
    method: 'POST',
    body: JSON.stringify({ issue_ids: issueIds, dry_run: false, allow_production: true }),
  })
}

export function markMaterialIssueAssembled(issueId: number) {
  return api(`/v1/production-control/material-issues/${issueId}/assembled`, {
    method: 'POST',
    body: JSON.stringify({ allow_production: true }),
  })
}

export function syncPostedTransfers() {
  return api<Record<string, unknown>>('/v1/production-control/sync-posted-transfers', {
    method: 'POST',
  })
}

export function closePaintWeldChain(payload: PaintWeldChainCloseRequest) {
  return api<PaintWeldChainResult>('/v1/paint-weld/chain/close', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateOrderQuantity(productId: number, quantity: number) {
  return api<OrderQuantityUpdateResult>(`/v1/production-control/orders/${productId}/quantity`, {
    method: 'PATCH',
    body: JSON.stringify({ quantity }),
  })
}

export function produceOrder(productId: number, payload: ProduceOrderRequest) {
  return api<Record<string, unknown>>(`/v1/production-control/orders/${productId}/produce`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function exportManufacturesTo1C(manufactureIds: number[], dryRun: boolean, allowProduction: boolean) {
  return api<Record<string, unknown>>('/v1/production-control/manufactures/export-to-1c', {
    method: 'POST',
    body: JSON.stringify({ manufacture_ids: manufactureIds, dry_run: dryRun, allow_production: allowProduction }),
  })
}

export function exportManufacturesPieceworkTo1C(manufactureIds: number[]) {
  return api<Record<string, unknown>>('/v1/production-control/manufactures/export-piecework-to-1c', {
    method: 'POST',
    body: JSON.stringify({ manufacture_ids: manufactureIds, dry_run: false, allow_production: true }),
  })
}

export function rollbackManufactureLocal(manufactureId: number) {
  return api(`/v1/production-control/manufactures/${manufactureId}/rollback-local`, { method: 'POST' })
}

export function createOrdersFromMrpRequirements(requirementIds: number[], initiatedBy: string) {
  return api<Record<string, unknown>>('/v1/production-control/orders/from-mrp-requirements', {
    method: 'POST',
    body: JSON.stringify({ requirement_ids: requirementIds, initiated_by: initiatedBy }),
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

export function deleteProductionOrder(productId: number) {
  return api(`/v1/production-control/orders/${productId}`, { method: 'DELETE' })
}
