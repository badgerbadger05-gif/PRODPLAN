import type { TableColumnDoctype } from '../tableDoctype'

export const transferRequestColumns = [
  { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, grow: false, align: 'center', sortable: false },
  { key: 'issue', title: 'Заявка', className: 'orderCell', width: 210, minWidth: 210, grow: false, align: 'left', sortable: false },
  { key: 'order', title: 'Заказ', className: 'orderCell', width: 210, minWidth: 210, grow: false, align: 'left', sortable: false },
  { key: 'item', title: 'Деталь', className: 'itemCell', width: undefined, minWidth: 300, grow: true, align: 'left', sortable: false },
  { key: 'quantity', title: 'Кол-во', className: 'numCell', width: 100, minWidth: 100, grow: false, align: 'right', sortable: false },
  { key: 'source_warehouse', title: 'Склад', className: undefined, width: 190, minWidth: 190, grow: false, align: 'left', sortable: false },
  { key: 'one_c', title: '1С', className: undefined, width: 210, minWidth: 210, grow: false, align: 'left', sortable: false },
  { key: 'status', title: 'Статус', className: undefined, width: 210, minWidth: 210, grow: false, align: 'left', sortable: false },
] as const satisfies TableColumnDoctype[]
