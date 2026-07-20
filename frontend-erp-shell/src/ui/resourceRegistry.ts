import type { Role } from './doctype'

export type FrontendResource = {
  name: string
  to: string
  title: string
  end?: boolean
  view: Role[]
  kind: 'doctype' | 'custom'
}

export const frontendResources: FrontendResource[] = [
  { name: 'home', to: '/', title: 'Главная', end: true, view: ['viewer', 'planner', 'buyer', 'shopfloor', 'admin'], kind: 'custom' },
  { name: 'period_plan', to: '/period-plan', title: 'Планирование выпуска', view: ['planner', 'admin'], kind: 'custom' },
  { name: 'dbr', to: '/dbr', title: 'Планирование DBR', view: ['planner', 'admin'], kind: 'custom' },
  { name: 'plan_run', to: '/mrp-runs', title: 'MRP прогоны', view: ['viewer', 'planner', 'admin'], kind: 'doctype' },
  { name: 'production_order', to: '/production-control', title: 'Журнал заказов', view: ['planner', 'shopfloor', 'admin'], kind: 'custom' },
  { name: 'purchase_order', to: '/purchase-control', title: 'Журнал закупок', view: ['planner', 'buyer', 'admin'], kind: 'custom' },
  { name: 'material_transfer', to: '/transfer-requests', title: 'Заявки перемещений', view: ['viewer', 'planner', 'shopfloor', 'admin'], kind: 'doctype' },
  { name: 'production_report', to: '/production-report-week', title: 'Выпуск недельный', view: ['viewer', 'planner', 'shopfloor', 'admin'], kind: 'custom' },
  { name: 'resources', to: '/resources', title: 'Ресурсы', view: ['viewer', 'planner', 'admin'], kind: 'custom' },
  { name: 'workshop_binding', to: '/workshop-binding-review', title: 'Разбор привязок', view: ['planner', 'admin'], kind: 'custom' },
  { name: 'stage_distribution', to: '/stage-distribution', title: 'Распределение этапов', view: ['planner', 'admin'], kind: 'custom' },
  { name: 'specification', to: '/specification', title: 'Спецификации', view: ['viewer', 'planner', 'admin'], kind: 'custom' },
  { name: 'sync', to: '/sync', title: 'Синхронизация', view: ['admin'], kind: 'custom' },
]

