import type {
  ControlSettings,
  EmployeesResponse,
  MaterialIssueDetail,
  MaterialIssueCreateResponse,
  MaterialIssueCreatePayload,
  MaterialsResponse,
  PaintWeldChainClosePayload,
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

export type RootProductOption = ApiSchemas['ProductionControlRootProductOption']
export type RootProductOptionsResponse = ApiSchemas['ProductionControlRootProductOptionsResponse']

// Loosely-typed side of the paint↔weld chain preview/close response. The
// endpoint returns a heterogeneous document the page reads field-by-field.
export function listProductionOrders(params: URLSearchParams) {
  return api<ApiSchemas['ProductionOrderJournalResponse']>(`/v1/production-control/orders?${params.toString()}`)
}

export type MaterializedMakeProduct = {
  work_item_id: number
  product_id: number
  order_id: number
  order_number: string
  requirement_id: number
  qty: number
}

export type MaterializeMakeWorkItemsResponse = {
  status: string
  created: MaterializedMakeProduct[]
  reused: MaterializedMakeProduct[]
}

export type MakeLaunchRequest = {
  work_item_id: number
  launch_qty: number
  expected_materialized_qty: number
}

export function materializeMakeWorkItems(workItems: number[] | MakeLaunchRequest[]) {
  const legacyIds = workItems.filter((row): row is number => typeof row === 'number')
  const requests = workItems.filter((row): row is MakeLaunchRequest => typeof row !== 'number')
  const body = {
    work_item_ids: legacyIds,
    work_items: requests,
    initiated_by: 'erp-shell',
  }
  return api<MaterializeMakeWorkItemsResponse>('/v1/production-control/orders/from-work-items', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export type OrderQuantityUpdateResult = {
  status: string
  product_id: number
  order_id: number
  previous_quantity: number
  quantity: number
  remaining_qty: number
  launchable_qty: number
  material_issues_open: number
}

// Количество уже созданного, но ещё не открытого в 1С заказа. Потребность
// компонентов пересчитывается на бэкенде от нового количества.
export function updateOrderQuantity(productId: number, quantity: number) {
  return api<OrderQuantityUpdateResult>(`/v1/production-control/orders/${productId}/quantity`, {
    method: 'PATCH',
    body: JSON.stringify({ quantity, initiated_by: 'erp-shell' }),
  })
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

export function listRootProductOptions() {
  return api<RootProductOptionsResponse>('/v1/production-control/orders/root-products')
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

export function getWorkItemMaterials(workItemId: number, quantity: number, ledgerGenerationId: number) {
  const params = new URLSearchParams({
    qty: String(quantity),
    ledger_generation_id: String(ledgerGenerationId),
  })
  return api<MaterialsResponse>(`/v1/production-control/work-items/${workItemId}/materials?${params}`)
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

export type ExecutionSyncResult = {
  orders: Record<string, unknown>
  transfers: Record<string, unknown>
}

export function syncExecutionFrom1C() {
  return api<ExecutionSyncResult>('/v1/production-control/sync-execution-from-1c', {
    method: 'POST',
  })
}

export function produceOrderLine(productId: number, payload: ProduceLinePayload) {
  return api<ProduceLineResult>(`/v1/production-control/orders/${productId}/produce`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export type OpenPaintWeldChainsResult = {
  status: string
  product_ids: number[]
  entries: Array<Record<string, unknown>>
  errors: Array<Record<string, unknown>>
}

export function openPaintWeldChains(productIds: number[]) {
  return api<OpenPaintWeldChainsResult>('/v1/production-control/orders/open-paint-weld-chains', {
    method: 'POST',
    body: JSON.stringify({ product_ids: productIds, initiated_by: 'erp-shell' }),
  })
}

export type ClosePaintWeldChainResult = {
  status: string
  message?: string | null
  resume_required?: boolean
  painted?: Record<string, unknown>
  welded?: Record<string, unknown>
}

// Исполнители обеих сторон цепочки выбираются оператором до записи в 1С:
// 1С не проводит сдельный наряд с пустой строкой регистра «Сдельные наряды»,
// а комбинированный наряд цепочки несёт строки сварки и окраски сразу.
export function closePaintWeldChain(productId: number, payload: PaintWeldChainClosePayload = {}) {
  return api<ClosePaintWeldChainResult>('/v1/paint-weld/chain/close', {
    method: 'POST',
    body: JSON.stringify({
      product_id: productId,
      dry_run: false,
      initiated_by: 'erp-shell',
      ...payload,
    }),
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

export function updateItem(itemId: number, payload: ApiSchemas['ItemPatch']) {
  return api(`/v1/items/${itemId}`, {
    method: 'PATCH',
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

export type CloseProductionOrderResult = {
  status: string
  dry_run: boolean
  orders_requested: number
  orders_eligible: number
  orders_closed: number
  orders_error: number
}

export function closeProductionOrder(productId: number, payload: { dry_run?: boolean } = {}) {
  return api<CloseProductionOrderResult>(`/v1/production-control/orders/${productId}/close`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}
