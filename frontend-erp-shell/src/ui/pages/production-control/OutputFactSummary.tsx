type OutputFactSummaryProps = {
  scope: 'plan' | 'order' | 'proposal'
  planned: number | null | undefined
  accepted: number | null | undefined
  remaining: number | null | undefined
  overproduction?: number | null
  compact?: boolean
  available?: boolean
}

function outputFactValue(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('ru-RU', { maximumFractionDigits: 3 })
    : 'Недоступно'
}

export function OutputFactSummary({ scope, planned, accepted, remaining, overproduction, compact = false, available = true }: OutputFactSummaryProps) {
  const className = compact ? 'outputFactSummary outputFactSummaryCompact' : 'outputFactSummary'
  const plannedLabel = scope === 'plan' ? 'Исходный план' : scope === 'proposal' ? 'Предложено MRP' : 'Заказано'
  const remainingLabel = scope === 'plan' ? 'Осталось выпустить' : scope === 'proposal' ? 'Осталось предложения' : 'Осталось по заказу'
  const ariaLabel = scope === 'plan' ? 'Факт выпуска плана' : scope === 'proposal' ? 'Факт предложения MRP' : 'Факт выпуска заказа'

  return (
    <div className={className} aria-label={ariaLabel}>
      <span><b>{plannedLabel}</b> {outputFactValue(scope === 'plan' && !available ? undefined : planned)}</span>
      <span><b>Принято Ledger</b> {outputFactValue(available ? accepted : undefined)}</span>
      <span><b>{remainingLabel}</b> {outputFactValue(available ? remaining : undefined)}</span>
      {scope === 'plan' && available && typeof overproduction === 'number' && overproduction > 0 && (
        <span><b>Сверх скорректированного плана</b> {outputFactValue(overproduction)}</span>
      )}
    </div>
  )
}
