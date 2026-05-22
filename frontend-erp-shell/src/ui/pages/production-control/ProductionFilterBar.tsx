import { productionStatuses, type ProductionFilters } from '../../../domain/productionControl'
import type { ProductionResource } from '../../../domain/resources'

type Props = {
  filters: ProductionFilters
  resources: ProductionResource[]
  onChange: (filters: ProductionFilters) => void
  onSubmit: () => void
}

export function ProductionFilterBar({ filters, resources, onChange, onSubmit }: Props) {
  return (
    <div className="requisites">
      <label>
        <span>Поиск</span>
        <input value={filters.search} onChange={(e) => onChange({ ...filters, search: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && onSubmit()} />
      </label>
      <label>
        <span>Статус</span>
        <select value={filters.status} onChange={(e) => onChange({ ...filters, status: e.target.value })}>
          <option value="">Все</option>
          {productionStatuses.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
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
        <span>Открыт с</span>
        <input type="date" value={filters.date_from} onChange={(e) => onChange({ ...filters, date_from: e.target.value })} />
      </label>
      <label>
        <span>Открыт по</span>
        <input type="date" value={filters.date_to} onChange={(e) => onChange({ ...filters, date_to: e.target.value })} />
      </label>
      <button className="filterBtn" onClick={onSubmit}>Сформировать</button>
    </div>
  )
}
