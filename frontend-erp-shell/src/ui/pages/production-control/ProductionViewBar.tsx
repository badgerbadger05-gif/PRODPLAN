import type { ProductionControlView } from '../../../domain/productionControl'

type Props = {
  view: ProductionControlView
  onChange: (view: ProductionControlView) => void
}

const views = [
  { value: 'orders', label: 'Все заказы' },
  { value: 'mechshop', label: 'Очередь мехцеха' },
  { value: 'drum', label: 'Барабан сборки' },
] as const

export function ProductionViewBar({ view: activeView, onChange }: Props) {
  return (
    <div className="productionViewBar" aria-label="Представление журнала">
      <span className="productionViewLabel">Представление</span>
      <div className="productionViewTabs" role="group" aria-label="Контур планирования">
        {views.map((view) => (
          <button
            key={view.value}
            type="button"
            className={activeView === view.value ? 'active' : ''}
            aria-pressed={activeView === view.value}
            onClick={() => onChange(view.value)}
          >
            {view.label}
          </button>
        ))}
      </div>
      {activeView === 'mechshop' && (
        <span className="productionViewHint">MRP-потребность; приоритет барабана появится через настроенные полки</span>
      )}
      {activeView === 'drum' && (
        <span className="productionViewHint">Единственный календарный барабан · readiness gate сохранён на плитках</span>
      )}
    </div>
  )
}
