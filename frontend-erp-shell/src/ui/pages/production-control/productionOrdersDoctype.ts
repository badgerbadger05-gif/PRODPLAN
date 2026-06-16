import type { TableColumnDoctype } from '../../tableDoctype'

export const productionOrderColumns = [
  { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, grow: false, align: 'center', sortable: false },
  { key: 'order', title: 'Заказ', className: 'orderCell productionOrderNumberCell', minWidth: 182, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'item', title: 'Деталь', className: 'itemCell', width: undefined, minWidth: 340, grow: true, align: 'left', sortable: false },
  { key: 'quantity', title: 'Кол-во', className: 'numCell', minWidth: 66, autoWidth: true, grow: false, align: 'right', sortable: false },
  { key: 'planned_start_date', title: 'План', className: 'dateCell', minWidth: 110, autoWidth: true, grow: false, align: 'left', sortable: true },
  { key: 'workshop', title: 'Участок', className: undefined, minWidth: 150, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'status', title: 'Статус', className: undefined, minWidth: 136, autoWidth: true, grow: false, align: 'left', sortable: false },
  { key: 'coverage', title: 'Обеспечение', className: undefined, minWidth: 118, autoWidth: true, grow: false, align: 'left', sortable: false },
] as const satisfies TableColumnDoctype[]

export type ProductionOrderColumnKey = typeof productionOrderColumns[number]['key']
export type ProductionOrderSortKey = Extract<ProductionOrderColumnKey, 'planned_start_date'>
