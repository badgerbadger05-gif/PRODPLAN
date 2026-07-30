import type { Role } from './doctype'

export type FrontendResource = {
  name: string
  to: string
  title: string
  end?: boolean
  view: Role[]
  kind: 'doctype' | 'custom'
  shortcut?: string
}

export const frontendResources: FrontendResource[] = [
  { name: 'home', to: '/', title: 'Главная', end: true, view: ['viewer', 'planner', 'buyer', 'shopfloor', 'admin'], kind: 'custom', shortcut: 'Alt+1' },
  { name: 'period_plan', to: '/period-plan', title: 'Планирование выпуска', view: ['planner', 'admin'], kind: 'custom', shortcut: 'Alt+2' },
  { name: 'plan_run', to: '/mrp-runs', title: 'MRP прогоны', view: ['viewer', 'planner', 'admin'], kind: 'doctype', shortcut: 'Alt+3' },
  { name: 'production_order', to: '/production-control', title: 'Журнал заказов', view: ['planner', 'shopfloor', 'admin'], kind: 'custom', shortcut: 'Alt+4' },
  { name: 'purchase_order', to: '/purchase-control', title: 'Журнал закупок', view: ['planner', 'buyer', 'admin'], kind: 'doctype', shortcut: 'Alt+5' },
  { name: 'material_transfer', to: '/transfer-requests', title: 'Заявки перемещений', view: ['viewer', 'planner', 'shopfloor', 'admin'], kind: 'doctype', shortcut: 'Alt+6' },
  { name: 'ledger', to: '/ledger', title: 'Ledger', view: ['viewer', 'planner', 'buyer', 'admin'], kind: 'custom', shortcut: 'Alt+7' },
  { name: 'resources', to: '/resources', title: 'Ресурсы', view: ['viewer', 'planner', 'admin'], kind: 'custom', shortcut: 'Alt+8' },
  { name: 'workshop_binding', to: '/workshop-binding-review', title: 'Разбор привязок', view: ['planner', 'admin'], kind: 'doctype' },
  { name: 'stage_distribution', to: '/stage-distribution', title: 'Распределение этапов', view: ['planner', 'admin'], kind: 'custom' },
  { name: 'specification', to: '/specification', title: 'Спецификации', view: ['viewer', 'planner', 'admin'], kind: 'custom' },
  { name: 'sync', to: '/sync', title: 'Синхронизация', view: ['admin'], kind: 'custom' },
]

export function canAccessResource(resource: FrontendResource, roles: Role[]) {
  return resource.view.some((role) => roles.includes(role))
}
