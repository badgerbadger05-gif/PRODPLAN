import { coverageLabels, productionStatusOptions, type ProductionFilters } from '../../../domain/productionControl'
import type { ProductionResource } from '../../../domain/resources'
import type { ProductionOrderSortKey } from './productionOrdersDoctype'

type Props = {
  filters: ProductionFilters
  resources: ProductionResource[]
  onChange: (filters: ProductionFilters) => void
  onSubmit: () => void
  onToggleSort: (key: ProductionOrderSortKey) => void
}

const coverageOptions = ['shortage', 'partial', 'ready', 'to_move', 'assembled', 'in_progress', 'done', 'completed'] as const

export function ProductionFilterBar({ filters, resources, onChange, onSubmit, onToggleSort }: Props) {
  return (
    <div className="requisites productionFilters">
      <label>
        <span>Поиск</span>
        <input value={filters.search} onChange={(e) => onChange({ ...filters, search: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && onSubmit()} />
      </label>
      <label>
        <span>Статус</span>
        <select value={filters.status} onChange={(e) => onChange({ ...filters, status: e.target.value })}>
          <option value="">Все</option>
          {productionStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>
        <span>Участок</span>
        <select value={filters.workshop_id} onChange={(e) => onChange({ ...filters, workshop_id: e.target.value })}>
          <option value="">Все</option>
          {resources.map((row) => <option key={row.resource_id} value={row.resource_id}>{row.resource_name}</option>)}
        </select>
      </label>
      <label>
        <span>Обеспечение</span>
        <select value={filters.coverage_status} onChange={(e) => onChange({ ...filters, coverage_status: e.target.value })}>
          <option value="">Все</option>
          {coverageOptions.map((value) => <option key={value} value={value}>{coverageLabels[value]}</option>)}
        </select>
      </label>
      <button className="filterBtn sortFilterBtn" onClick={() => onToggleSort('planned_start_date')}>
        План {filters.sort_dir === 'asc' ? '▲' : '▼'}
      </button>
      <button className="filterBtn" onClick={onSubmit}>Сформировать</button>
    </div>
  )
}
