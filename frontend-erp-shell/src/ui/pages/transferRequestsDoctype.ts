import type { TableColumnDoctype } from '../tableDoctype'

export const transferRequestColumns = [
  { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, grow: false, align: 'center', sortable: false },
  { key: 'issue', title: 'Заявка', className: 'orderCell', minWidth: 116, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'order', title: 'Заказ', className: 'orderCell', minWidth: 112, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'item', title: 'Деталь', className: 'itemCell', width: undefined, minWidth: 300, grow: true, align: 'left', sortable: false },
  { key: 'quantity', title: 'Кол-во', className: 'numCell', minWidth: 64, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'source_warehouse', title: 'Склад', className: undefined, minWidth: 150, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'one_c', title: '1С', className: undefined, minWidth: 128, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'status', title: 'Статус', className: undefined, minWidth: 92, autoWidth: true, grow: false, align: 'left', sortable: false },
] as const satisfies TableColumnDoctype[]
