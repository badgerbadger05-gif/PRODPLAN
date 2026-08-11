import type { ProductionFilters } from '../../../domain/productionControl'

type Props = {
  filters: ProductionFilters
  onChange: (filters: ProductionFilters, submit?: boolean) => void
}

const views = [
  { value: '', label: 'Все заказы' },
  { value: 'mrp', label: 'Очередь мехцеха' },
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
              sort_by: 'planned_start_date',
              sort_dir: 'asc',
            }, true)}
          >
            {view.label}
          </button>
        ))}
      </div>
      {filters.planning_contour === 'mrp' && (
        <span className="productionViewHint">Очередь мехцеха · единый журнал запуска</span>
      )}
    </div>
  )
}
