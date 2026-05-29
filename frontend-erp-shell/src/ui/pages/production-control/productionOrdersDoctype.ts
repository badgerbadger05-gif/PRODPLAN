import type { TableColumnDoctype } from '../../tableDoctype'

export const productionOrderColumns = [
  { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, grow: false, align: 'center', sortable: false },
  { key: 'order', title: 'Заказ', className: 'orderCell', width: 136, minWidth: 136, grow: false, align: 'left', sortable: false },
  { key: 'item', title: 'Деталь', className: 'itemCell', width: undefined, minWidth: 280, grow: true, align: 'left', sortable: false },
  { key: 'quantity', title: 'Кол-во', className: 'numCell', width: 96, minWidth: 96, grow: false, align: 'right', sortable: false },
  { key: 'planned_start_date', title: 'План', className: 'dateCell', width: 132, minWidth: 132, grow: false, align: 'left', sortable: true },
  { key: 'workshop', title: 'Участок', className: undefined, width: 176, minWidth: 176, grow: false, align: 'left', sortable: false },
  { key: 'status', title: 'Статус', className: undefined, width: 176, minWidth: 176, grow: false, align: 'left', sortable: false },
  { key: 'coverage', title: 'Обеспечение', className: undefined, width: 148, minWidth: 148, grow: false, align: 'left', sortable: false },
] as const satisfies TableColumnDoctype[]

export type ProductionOrderColumnKey = typeof productionOrderColumns[number]['key']
export type ProductionOrderSortKey = Extract<ProductionOrderColumnKey, 'planned_start_date'>
