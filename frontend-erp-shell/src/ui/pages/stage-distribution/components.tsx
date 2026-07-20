import type { ResourceDistributionResult } from '../../../domain/stageDistribution'
import { qty } from '../../../lib/format'
import type { StageDistributionComponent } from './model'

type StageDistributionControlsProps = {
  aggregate: boolean
  loading: boolean
  onCalculate: () => void
  onAggregateChange: (aggregate: boolean) => void
}

export function StageDistributionControls({
  aggregate,
  loading,
  onCalculate,
  onAggregateChange,
}: StageDistributionControlsProps) {
  return (
    <div className="commandBar">
      <button className="primary" onClick={onCalculate} disabled={loading}>Рассчитать</button>
      <label className="inlineControl">
        <input type="checkbox" checked={aggregate} onChange={(event) => onAggregateChange(event.target.checked)} />
        <span>Суммировать одинаковые детали</span>
      </label>
    </div>
  )
}

type ResourceTabsProps = {
  resources: ResourceDistributionResult[]
  activeResourceId: number | null
  onActivate: (resourceId: number) => void
}

export function ResourceTabs({
  resources,
  activeResourceId,
  onActivate,
}: ResourceTabsProps) {
  return (
    <div className="tabsBar">
      {resources.map((resource) => (
        <button
          key={resource.resource_id}
          className={resource.resource_id === activeResourceId ? 'activeTab' : ''}
          onClick={() => onActivate(resource.resource_id)}
        >
          {resource.resource_name} · {qty(resource.norm_hours)} н/ч
        </button>
      ))}
    </div>
  )
}

type StageDistributionTableProps = {
  components: StageDistributionComponent[]
  hasResources: boolean
  loading: boolean
}

export function StageDistributionTable({
  components,
  hasResources,
  loading,
}: StageDistributionTableProps) {
  return (
    <div className="tablePane resultTablePane">
      <table className="journalTable stageDistributionTable">
        <thead>
          <tr>
            <th>Деталь</th>
            <th>Артикул</th>
            <th>Этап</th>
            <th>Кол-во</th>
            <th>Остаток</th>
            <th>Норма</th>
            <th>Сумма н/ч</th>
          </tr>
        </thead>
        <tbody>
          {components.map((row, index) => (
            <tr key={`${row.item_id}-${row.stage_id ?? 'x'}-${index}`}>
              <td className="itemCell">
                <strong>{row.item_name}</strong>
                <span>{row.item_code}</span>
              </td>
              <td>{row.item_article || ''}</td>
              <td>{row.stage_name || '—'}</td>
              <td className="numCell"><strong>{qty(row.qty_per_unit)}</strong></td>
              <td className="numCell"><strong>{qty(row.stock_qty)}</strong></td>
              <td className="numCell"><strong>{qty(row.norm_hours)}</strong></td>
              <td className="numCell"><strong>{qty(row.norm_hours_total)}</strong></td>
            </tr>
          ))}
        </tbody>
      </table>
      {!hasResources && !loading && <div className="emptyDetail">Нажмите «Рассчитать», чтобы получить распределение</div>}
    </div>
  )
}
