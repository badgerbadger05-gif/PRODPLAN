import type { PurchaseFilters, PurchaseRow } from '../../../domain/purchaseControl'
import { listPurchaseJournal } from '../../../services/purchaseControl'
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
      subtitle: 'Заказы поставщику из 1С и незаказанные MRP-потребности: сроки, поступления, просрочка',
      hotkeys: 'F5 Обновить · Enter Детали',
      idField: 'row_key',
      selectionMode: 'multiple',
      exportCsv: { filename: 'purchase_orders.csv' },
    },
    initialFilters: {
      search: focus.search ?? '',
      supplier_id: '',
      line_status: '',
      state: '',
      phase: '',
      active_only: true,
      sort_by: 'delivery_date',
      sort_dir: 'asc',
    },
    dataSource: {
      async list({ limit, offset, filters, sortBy, sortDir }) {
        const params = new URLSearchParams({
          limit: String(limit),
          offset: String(offset),
          active_only: filters.active_only ? 'true' : 'false',
          sort_by: sortBy === 'order' ? 'order_date' : sortBy === 'delivery_date' ? 'delivery_date' : filters.sort_by,
          sort_dir: sortDir ?? filters.sort_dir,
        })
        if (focus.orderId) params.set('order_id', focus.orderId)
        if (filters.search) params.set('search', filters.search)
        if (filters.supplier_id) params.set('supplier_id', filters.supplier_id)
        if (filters.line_status) params.set('line_status', filters.line_status)
        if (filters.state) params.set('state', filters.state)
        if (filters.phase) params.set('phase', filters.phase)
        return listPurchaseJournal(params)
      },
    },
    // The journal keeps its proven dense table renderer. These declarations remain
    // the shared sizing/sorting contract and make the resource discoverable by the ERP registry.
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
    ],
    permissions: {
      view: ['viewer', 'buyer', 'planner', 'admin'],
      actions: {
        export_1c: 'purchase.export_1c',
        sync_1c: 'purchase.sync_1c',
      },
    },
  }
}
