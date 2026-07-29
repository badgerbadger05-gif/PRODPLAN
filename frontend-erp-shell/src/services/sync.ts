import type { NomenclatureGroupItem, ODataConfig, SyncAction, WarehouseItem } from '../domain/sync'
import { api } from '../lib/api'

function syncPayload(config: ODataConfig, action: SyncAction) {
  return {
    ...config,
    entity_name: action.entity_name,
    dry_run: false,
    zero_missing: false,
  }
}

export function getODataConfig() {
  return api<ODataConfig>('/v1/odata/config')
}

export function saveODataConfig(config: ODataConfig) {
  return api<{ status: string; config: ODataConfig }>('/v1/odata/config', {
    method: 'POST',
    body: JSON.stringify(config),
  })
}

export function testODataConnection(config: ODataConfig) {
  return api<Record<string, unknown>>('/v1/odata/test', {
    method: 'POST',
    body: JSON.stringify(config),
  })
}

export function fetchODataMetadata(config: ODataConfig) {
  return api<Record<string, unknown>>('/v1/odata/metadata', {
    method: 'POST',
    body: JSON.stringify(config),
  })
}

export function runSyncAction(config: ODataConfig, action: SyncAction) {
  return api<Record<string, unknown>>(action.endpoint, {
    method: 'POST',
    body: JSON.stringify(syncPayload(config, action)),
  })
}

export function listWarehouses() {
  return api<{ rows: WarehouseItem[]; total: number; selected_total: number }>('/v1/sync/warehouses')
}

export function saveWarehouseSelection(selected_refs: string[]) {
  return api<Record<string, unknown>>('/v1/sync/warehouses/selection', {
    method: 'POST',
    body: JSON.stringify({ selected_refs }),
  })
}

export function listNomenclatureGroups() {
  return api<{ items?: NomenclatureGroupItem[]; rows?: NomenclatureGroupItem[]; selected_ids?: string[] }>('/v1/odata/groups')
}

export function saveNomenclatureGroupSelection(ids: string[]) {
  return api<Record<string, unknown>>('/v1/odata/groups/selection', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })
}

export function exportProductionOrdersReport() {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>('/v1/sync/production-orders-odata/export')
}

export function exportSupplierOrdersReport() {
  return api<{ data_base64?: string; filename?: string; content_type?: string }>('/v1/sync/supplier-orders-odata/export')
}
