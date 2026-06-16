import { supplyPhaseOptions, type PurchaseJournalSummary } from '../../../domain/purchaseControl'

type Props = {
  selectedCount: number
  toOrderCount: number
  summary: PurchaseJournalSummary | null
  activePhase: string
  loading: boolean
  onOrderTo1C: () => void
  onSyncFrom1C: () => void
  onDownloadCsv: () => void
  onRefresh: () => void
  onSelectAllToOrder: () => void
  onClearSelection: () => void
  onShowStatus: (status: string) => void
  onShowPhase: (phase: string) => void
}

export function PurchaseCommandBar({
  selectedCount,
  toOrderCount,
  summary,
  activePhase,
  loading,
  onOrderTo1C,
  onSyncFrom1C,
  onDownloadCsv,
  onRefresh,
  onSelectAllToOrder,
  onClearSelection,
  onShowStatus,
  onShowPhase,
}: Props) {
  return (
    <div className="commandBar">
      <button
        className="primary"
        onClick={onOrderTo1C}
        disabled={!selectedCount || loading}
        title={selectedCount ? 'Создать заказы поставщику в 1С по выбранным MRP-потребностям (один документ на поставщика)' : 'Выберите чекбоксами строки «К заказу»'}
      >
        Заказать в 1С{selectedCount ? ` (${selectedCount})` : ''}
      </button>
      <button onClick={onSyncFrom1C} disabled={loading} title="Загрузить из 1С статусы заказов и факт поступления">Синхронизировать</button>
      <div className="barSeparator" />
      <button onClick={onSelectAllToOrder} disabled={!toOrderCount}>Выбрать все «К заказу»</button>
      <button onClick={onClearSelection} disabled={!selectedCount}>Снять выбор</button>
      <div className="barSeparator" />
      <button onClick={onDownloadCsv} disabled={loading}>CSV</button>
      <button onClick={onRefresh} disabled={loading}>Обновить</button>
      {summary && (
        <>
          <div className="barSeparator" />
          {supplyPhaseOptions.map(([value, label]) => (
            <button
              key={value}
              className="filterBtn"
              onClick={() => onShowPhase(value)}
              style={activePhase === value ? { fontWeight: 700, textDecoration: 'underline' } : undefined}
              title={`Показать только фазу «${label}»`}
            >
              {label}: {summary.by_phase?.[value] ?? 0}
            </button>
          ))}
          <div className="barSeparator" />
          <button className="filterBtn" onClick={() => onShowStatus('to_order')} title="Показать только строки «К заказу»">
            К заказу: {summary.to_order}
          </button>
          <button
            className="filterBtn"
            onClick={() => onShowStatus('overdue')}
            style={summary.overdue > 0 ? { color: 'var(--red)' } : undefined}
            title="Показать только просроченные строки"
          >
            Просрочено: {summary.overdue}
          </button>
          <span className="toolbarText" title="Строки с датой поставки в ближайшие 7 дней">Ожидается за 7 дн: {summary.expected_7d}</span>
        </>
      )}
    </div>
  )
}
