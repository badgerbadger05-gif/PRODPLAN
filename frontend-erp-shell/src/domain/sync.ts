export type ODataConfig = {
  base_url: string
  username?: string
  password?: string
  token?: string
}

export type SyncActionId =
  | 'nomenclature'
  | 'warehouses'
  | 'stock'
  | 'productionKinds'
  | 'employees'
  | 'brigades'
  | 'operations'
  | 'specifications'
  | 'defaultSpecifications'
  | 'productionStages'
  | 'productionOrders'
  | 'productionFacts'
  | 'supplierOrders'

export type SyncAction = {
  id: SyncActionId
  title: string
  group: 'Справочники' | 'Структура производства' | 'Склад и заказы'
  endpoint: string
  entity_name?: string
  note?: string
}

export type WarehouseItem = {
  warehouse_id: number
  warehouse_ref1c: string
  warehouse_code: string
  warehouse_name: string
  is_selected: boolean
}

// GET /v1/odata/groups replays the cached 1C payload verbatim:
// `{"value": [{Ref_Key, Code, Description, IsFolder}]}` (backend
// routers/odata.py::get_saved_groups). The selection lives on a separate
// endpoint, GET /v1/odata/groups/selection → `{"ids": [...]}`.
export type NomenclatureGroupODataRow = {
  Ref_Key: string
  Code?: string | null
  Description?: string | null
  IsFolder?: boolean | null
}

export type NomenclatureGroupsResponse = {
  value?: NomenclatureGroupODataRow[] | null
}

export type NomenclatureGroupsSelectionResponse = {
  ids?: string[] | null
}

export type NomenclatureGroupItem = {
  id: string
  code: string
  name: string
}

// Single typed adapter for the raw 1C rows. Only folders are selectable; the
// cache is written with `IsFolder eq true`, so a missing flag is tolerated and
// only an explicit `false` is dropped.
export function toNomenclatureGroupItems(response: NomenclatureGroupsResponse): NomenclatureGroupItem[] {
  return (response.value ?? [])
    .filter((row) => row.IsFolder !== false)
    .map((row) => ({
      id: String(row.Ref_Key ?? '').trim(),
      code: String(row.Code ?? '').trim(),
      name: String(row.Description ?? '').trim(),
    }))
    .filter((row) => row.id !== '')
}

export type SyncLogEntry = {
  at: string
  title: string
  status: 'ok' | 'error' | 'running'
  details?: string
}

export const syncActions: SyncAction[] = [
  { id: 'nomenclature', title: 'Номенклатура + ЕИ', group: 'Справочники', endpoint: '/v1/sync/nomenclature-odata', entity_name: 'Catalog_Номенклатура' },
  { id: 'warehouses', title: 'Склады', group: 'Склад и заказы', endpoint: '/v1/sync/warehouses-odata', entity_name: 'AccumulationRegister_ЗапасыНаСкладах' },
  { id: 'stock', title: 'Остатки', group: 'Склад и заказы', endpoint: '/v1/sync/stock-odata', entity_name: 'AccumulationRegister_ЗапасыНаСкладах' },
  { id: 'productionKinds', title: 'Виды производства', group: 'Справочники', endpoint: '/v1/sync/production-kinds-odata', entity_name: 'Catalog_ВидыПроизводства' },
  { id: 'employees', title: 'Сотрудники', group: 'Справочники', endpoint: '/v1/sync/employees-odata', entity_name: 'Catalog_Сотрудники' },
  { id: 'brigades', title: 'Бригады', group: 'Справочники', endpoint: '/v1/sync/employees-odata', entity_name: 'Catalog_Бригады' },
  { id: 'operations', title: 'Операции', group: 'Структура производства', endpoint: '/v1/sync/operations-odata', entity_name: 'Catalog_Спецификации_Операции' },
  { id: 'specifications', title: 'Спецификации', group: 'Структура производства', endpoint: '/v1/sync/specifications-odata', entity_name: 'Catalog_Спецификации' },
  { id: 'defaultSpecifications', title: 'Спецификации по умолчанию', group: 'Структура производства', endpoint: '/v1/sync/default-specifications-odata', entity_name: 'InformationRegister_СпецификацииПоУмолчанию' },
  { id: 'productionStages', title: 'Этапы производства', group: 'Структура производства', endpoint: '/v1/sync/production-stages-odata', entity_name: 'Catalog_ЭтапыПроизводства' },
  { id: 'productionOrders', title: 'Заказы на производство', group: 'Склад и заказы', endpoint: '/v1/sync/production-orders-odata', entity_name: 'Document_ЗаказНаПроизводство' },
  { id: 'productionFacts', title: 'Факт выпуска', group: 'Склад и заказы', endpoint: '/v1/sync/production-orders-fact-odata', entity_name: 'Document_СборкаЗапасов' },
  { id: 'supplierOrders', title: 'Заказы поставщику', group: 'Склад и заказы', endpoint: '/v1/sync/supplier-orders-odata', entity_name: 'Document_ЗаказПоставщику' },
]

export const fullSyncOrder: SyncActionId[] = [
  'nomenclature',
  'productionKinds',
  'employees',
  'brigades',
  'operations',
  'specifications',
  'defaultSpecifications',
  'productionStages',
  'warehouses',
  'stock',
  'productionOrders',
  'productionFacts',
  'supplierOrders',
]
