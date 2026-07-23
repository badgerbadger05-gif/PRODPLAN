import type { ProductionFilters } from '../../../domain/productionControl'

type Props = {
  filters: ProductionFilters
  onChange: (filters: ProductionFilters, submit?: boolean) => void
}

const views = [
  { value: '', label: 'Все заказы' },
  { value: 'dbr_feeder', label: 'Очередь мехцеха' },
] as const

export function ProductionViewBar({ filters, onChange }: Props) {
  return (
    <div className="productionViewBar" aria-label="Представление журнала">
      <span className="productionViewLabel">Представление</span>
      <div className="productionViewTabs" role="group" aria-label="Контур планирования">
        {views.map((view) => (
          <button
            key={view.value || 'all'}
            type="button"
            className={filters.planning_contour === view.value ? 'active' : ''}
            aria-pressed={filters.planning_contour === view.value}
            onClick={() => onChange({
              ...filters,
              planning_contour: view.value,
              sort_by: view.value === 'dbr_feeder' ? 'dbr_priority' : 'planned_start_date',
              sort_dir: view.value === 'dbr_feeder' ? 'desc' : 'asc',
            }, true)}
          >
            {view.label}
          </button>
        ))}
      </div>
      {filters.planning_contour === 'dbr_feeder' && (
        <span className="productionViewHint">Приоритет DBR · единый журнал запуска</span>
      )}
    </div>
  )
}
