import type {
  NomenclatureGroupItem,
  NomenclatureGroupsResponse,
  NomenclatureGroupsSelectionResponse,
  ODataConfig,
  SyncAction,
  WarehouseItem,
} from '../domain/sync'
import { toNomenclatureGroupItems } from '../domain/sync'
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

export async function listNomenclatureGroups(): Promise<NomenclatureGroupItem[]> {
  const data = await api<NomenclatureGroupsResponse>('/v1/odata/groups')
  return toNomenclatureGroupItems(data)
}

export async function getNomenclatureGroupSelection(): Promise<string[]> {
  const data = await api<NomenclatureGroupsSelectionResponse>('/v1/odata/groups/selection')
  return (data.ids ?? []).map((id) => String(id))
}

// Re-pulls the folder list from 1C into output/odata_groups_nomenclature.json.
// This is the only action that refreshes the available groups; it never touches
// the saved selection.
export function refreshNomenclatureGroups(config: ODataConfig) {
  return api<{ status: string; total: number; file: string }>('/v1/odata/categories/export_groups', {
    method: 'POST',
    body: JSON.stringify(config),
  })
}

export function saveNomenclatureGroupSelection(ids: string[]) {
  return api<{ status: string; saved: number }>('/v1/odata/groups/selection', {
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
