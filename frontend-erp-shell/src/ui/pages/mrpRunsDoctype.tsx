import { planningStatusLabel, type PlanningRunRow } from '../../domain/planning'
import { dateRu, qty } from '../../lib/format'
import { listPlanningRuns } from '../../services/planning'
import type { Doctype } from '../doctype'

export type MrpRunsFilters = Record<string, never>

export function mrpPlanLabel(row?: PlanningRunRow | null) {
  if (!row) return '—'
  if (row.source_plan_name) return row.source_plan_name
  if (row.source_plan_id) return `План #${row.source_plan_id}`
  return 'скользящий план'
}

export function mrpPeriodLabel(row?: PlanningRunRow | null) {
  if (!row) return '—'
  const from = dateRu(row.period_from)
  const to = dateRu(row.period_to)
  if (from && to) return `${from} — ${to}`
  return from || to || '—'
}

export const mrpRunsDoctype: Doctype<PlanningRunRow, MrpRunsFilters, never> = {
  meta: {
    name: 'plan_run',
    title: 'MRP планирование',
    subtitle: 'Контрольные прогоны расчёта: производство, закупки и перегрузы',
    hotkeys: 'Enter Детали',
    idField: 'run_id',
    selectionMode: 'single',
    exportCsv: { filename: 'mrp_runs.csv' },
  },
  initialFilters: {},
  dataSource: {
    list: ({ limit, offset }, signal) => {
      void signal
      return listPlanningRuns({ limit, offset })
    },
  },
  columns: [
    {
      key: 'run_id',
      title: 'RUN',
      className: 'orderCell',
      width: 100,
      type: 'number',
      render: (row) => <><strong>#{row.run_id}</strong><span>расчёт</span></>,
    },
    {
      key: 'plan',
      title: 'План',
      className: 'orderCell',
      minWidth: 180,
      grow: true,
      render: (row) => <><strong>{mrpPlanLabel(row)}</strong>{row.source_plan_id && <span>план #{row.source_plan_id}</span>}</>,
    },
    {
      key: 'status',
      title: 'Статус',
      width: 120,
      render: (row) => <span className={`pill ${row.status.toLowerCase()}`}>{planningStatusLabel(row.status)}</span>,
    },
    {
      key: 'period',
      title: 'Период',
      width: 190,
      value: mrpPeriodLabel,
    },
    {
      key: 'order_count',
      title: 'Производство',
      className: 'numCell',
      width: 120,
      type: 'qty',
      render: (row) => <><strong>{qty(row.order_count)}</strong><span>заказов</span></>,
    },
    {
      key: 'purchase_count',
      title: 'Закупки',
      className: 'numCell',
      width: 110,
      type: 'qty',
      render: (row) => <><strong>{qty(row.purchase_count)}</strong><span>строк</span></>,
    },
    {
      key: 'overload_buckets',
      title: 'Перегрузы',
      className: 'numCell',
      width: 110,
      type: 'qty',
      render: (row) => <><strong>{qty(row.overload_buckets)}</strong><span>окон</span></>,
    },
  ],
  detail: { sections: [] },
  permissions: { view: ['viewer', 'planner', 'admin'] },
}
