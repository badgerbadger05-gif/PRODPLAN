import {
  purchaseLineStatusLabel,
  supplyPhaseLabel,
  type PurchaseFilters,
  type PurchaseRow,
} from '../../../domain/purchaseControl'
import {
  listPurchaseJournal,
  materializePurchaseControlRows,
  syncSupplierOrdersFrom1C,
} from '../../../services/purchaseControl'
import type { Doctype } from '../../doctype'
import type { TableColumnDoctype } from '../../tableDoctype'

export const purchaseOrderColumns = [
  { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, grow: false, align: 'center', sortable: false },
  { key: 'order', title: 'Заказ', className: 'orderCell purchaseOrderNumberCell', minWidth: 168, autoWidth: true, grow: false, align: 'left', sortable: true },
  { key: 'item', title: 'Номенклатура', className: 'itemCell purchaseItemCell', width: undefined, minWidth: 360, grow: true, align: 'left', sortable: false },
  { key: 'supplier', title: 'Поставщик', className: 'supplierCell', minWidth: 180, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'quantity', title: 'Кол-во', className: 'numCell', minWidth: 80, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'received', title: 'Поступило', className: 'numCell', minWidth: 84, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'delivery_date', title: 'Поставка', className: 'dateCell', minWidth: 110, autoWidth: true, grow: false, align: 'left', sortable: true },
  { key: 'state', title: 'Статус 1С', className: undefined, minWidth: 130, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'line_status', title: 'Статус', className: undefined, minWidth: 116, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'price', title: 'Учётная цена', className: 'numCell', minWidth: 104, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'amount', title: 'Сумма', className: 'numCell', minWidth: 90, autoWidth: true, grow: false, align: 'right', sortable: false },
] as const satisfies TableColumnDoctype[]

export type PurchaseOrderColumnKey = typeof purchaseOrderColumns[number]['key']
export type PurchaseOrderSortKey = Extract<PurchaseOrderColumnKey, 'delivery_date' | 'order'>

export function createPurchaseOrdersDoctype(
  focus: { orderId?: string | null; search?: string | null } = {},
): Doctype<PurchaseRow, PurchaseFilters> {
  return {
    meta: {
      name: 'purchase_order',
      title: 'Журнал закупок',
      subtitle: 'Общие BUY-потребности по планам: поступило, покрыто заказами и точный остаток к заказу',
      hotkeys: 'F5 Обновить · Enter Детали',
      idField: 'row_key',
      selectionMode: 'multiple',
      exportCsv: {
        filename: 'purchase-journal.csv',
        delimiter: ';',
        quote: 'all',
        lineEnding: '\n',
        rows: 'current-page',
        visibleColumnsOnly: false,
        columns: [
          {
            key: 'order',
            title: 'Заказ',
            value: (row) => row.row_generator === 'mrp_reservation'
              ? 'Под заказ (MRP)'
              : row.order_number,
          },
          {
            key: 'order_date',
            title: 'Дата заказа',
            value: (row) => row.order_date ?? '',
            permissionField: 'order',
          },
          { key: 'supplier', title: 'Поставщик', value: (row) => row.supplier_name },
          {
            key: 'article',
            title: 'Артикул',
            value: (row) => row.item_article ?? row.item_code,
            permissionField: 'item',
          },
          { key: 'item', title: 'Номенклатура', value: (row) => row.item_name },
          { key: 'quantity', title: 'Заказано', value: (row) => row.quantity },
          { key: 'received', title: 'Поступило', value: (row) => row.received_qty ?? 'н/д' },
          {
            key: 'remaining',
            title: 'Осталось',
            value: (row) => row.remaining_qty,
            permissionField: 'quantity',
          },
          { key: 'delivery_date', title: 'Дата поставки', value: (row) => row.delivery_date ?? row.need_date ?? '' },
          {
            key: 'overdue',
            title: 'Просрочка, дн',
            value: (row) => row.overdue_days || '',
            permissionField: 'delivery_date',
          },
          { key: 'state', title: 'Статус 1С', value: (row) => row.order_state_name ?? '' },
          {
            key: 'phase',
            title: 'Фаза',
            value: (row) => supplyPhaseLabel(row.supply_phase),
            permissionField: 'state',
          },
          { key: 'line_status', title: 'Статус', value: (row) => purchaseLineStatusLabel(row.line_status) },
          { key: 'price', title: 'Учётная цена', value: (row) => row.price ?? '' },
          { key: 'amount', title: 'Сумма', value: (row) => row.amount || '' },
        ],
      },
    },
    initialFilters: {
      search: focus.search ?? '',
      supplier_id: '',
      line_status: '',
      state: '',
      phase: '',
      active_only: true,
      include_to_order: true,
      horizon_period_to: '',
      sort_by: 'delivery_date',
      sort_dir: 'asc',
    },
    dataSource: {
      async list({ limit, offset, filters, sortBy, sortDir }) {
        const params = new URLSearchParams({
          limit: String(limit),
          offset: String(offset),
          active_only: filters.active_only ? 'true' : 'false',
          include_to_order: filters.include_to_order ? 'true' : 'false',
          sort_by: sortBy === 'order' ? 'order_date' : sortBy === 'delivery_date' ? 'delivery_date' : filters.sort_by,
          sort_dir: sortDir ?? filters.sort_dir,
        })
        if (focus.orderId) params.set('order_id', focus.orderId)
        if (filters.search) params.set('search', filters.search)
        if (filters.supplier_id) params.set('supplier_id', filters.supplier_id)
        if (filters.line_status) params.set('line_status', filters.line_status)
        if (filters.state) params.set('state', filters.state)
        if (filters.phase) params.set('phase', filters.phase)
        if (filters.horizon_period_to) params.set('horizon_period_to', filters.horizon_period_to)
        return listPurchaseJournal(params)
      },
    },
    columns: purchaseOrderColumns.map((column) => ({
      ...column,
      type: column.key === 'select' ? 'select-checkbox' as const : undefined,
    })),
    filters: [
      { kind: 'search', field: 'search', mode: 'submit' },
      { kind: 'select', field: 'supplier_id', label: 'Поставщик', options: [], allowEmpty: true },
      { kind: 'select', field: 'phase', label: 'Фаза', options: [], allowEmpty: true },
      { kind: 'toggle', field: 'active_only', label: 'Активные' },
      { kind: 'select', field: 'state', label: 'Статус 1С', options: [], allowEmpty: true },
      { kind: 'select', field: 'line_status', label: 'Статус', options: [], allowEmpty: true },
      { kind: 'toggle', field: 'include_to_order', label: 'К заказу' },
      { kind: 'search', field: 'horizon_period_to', mode: 'submit' },
    ],
    actions: [
      {
        key: 'materialize',
        label: ({ selection }) => `Сформировать заказы${selection.length ? ` (${selection.length})` : ''}`,
        scope: 'selection',
        tone: 'primary',
        disabledReason: ({ selection }) => selection.length
          ? ''
          : 'Выберите MRP-строки к заказу',
        async run({ selection, listMeta }) {
          const snapshotId = Number((listMeta.meta as { snapshot_id?: number } | undefined)?.snapshot_id ?? 0)
          if (!snapshotId) return { error: 'Снимок закупок ещё не зафиксирован' }
          const selectedRowKeys = [
            ...new Set(selection.map((row) => row.row_key)),
          ]
          if (!selectedRowKeys.length) return { error: 'В выбранных строках нет строк к заказу' }
          const result = await materializePurchaseControlRows({
            snapshot_id: snapshotId,
            row_keys: selectedRowKeys,
            dry_run: false,
          })
          const rowsTotal = Number((result as { rows_total?: number }).rows_total ?? selectedRowKeys.length)
          // Materialization creates the 1C command, but the journal is not
          // authoritative again until its readback succeeds.  Propagate a
          // failed sync instead of presenting a false completed state.
          await syncSupplierOrdersFrom1C()
          return {
            message: `Сформировано заказов по ${rowsTotal} строкам снапшота`,
            clearSelection: true,
            reload: true,
          }
        },
      },
      {
        key: 'sync_1c',
        label: 'Синхронизировать',
        scope: 'global',
        async run() {
          const stats = await syncSupplierOrdersFrom1C()
          return {
            message: `Синхронизация: новых заказов ${Number(stats.orders_created ?? 0)}, обновлено ${Number(stats.orders_updated ?? 0)}`,
            reload: true,
          }
        },
      },
    ],
    permissions: {
      view: ['viewer', 'buyer', 'planner', 'admin'],
      actions: {
        materialize: 'purchase.export_1c',
        sync_1c: 'purchase.sync_1c',
      },
    },
    selectable: (row) => row.can_materialize,
    selectionDisabledReason: (row) => row.materialize_disabled_reason || 'Строка недоступна для формирования заказа',
  }
}
