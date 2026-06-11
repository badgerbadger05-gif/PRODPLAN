import type { TableColumnDoctype } from '../../tableDoctype'

export const purchaseOrderColumns = [
  { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, grow: false, align: 'center', sortable: false },
  { key: 'order', title: 'Заказ', className: 'orderCell', minWidth: 132, autoWidth: true, grow: false, align: 'left', sortable: true },
  { key: 'item', title: 'Номенклатура', className: 'itemCell', width: undefined, minWidth: 280, grow: true, align: 'left', sortable: false },
  { key: 'supplier', title: 'Поставщик', className: undefined, minWidth: 150, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'quantity', title: 'Кол-во', className: 'numCell', minWidth: 80, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'received', title: 'Поступило', className: 'numCell', minWidth: 84, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'delivery_date', title: 'Поставка', className: 'dateCell', minWidth: 110, autoWidth: true, grow: false, align: 'left', sortable: true },
  { key: 'state', title: 'Статус 1С', className: undefined, minWidth: 130, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'line_status', title: 'Статус', className: undefined, minWidth: 116, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'amount', title: 'Сумма', className: 'numCell', minWidth: 90, autoWidth: true, grow: false, align: 'right', sortable: false },
] as const satisfies TableColumnDoctype[]

export type PurchaseOrderColumnKey = typeof purchaseOrderColumns[number]['key']
export type PurchaseOrderSortKey = Extract<PurchaseOrderColumnKey, 'delivery_date' | 'order'>
