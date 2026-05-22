type Props = {
  loading: boolean
  visibleFrom: number
  visibleTo: number
  total: number
  selectedCount: number
  canPrev: boolean
  canNext: boolean
  onPrev: () => void
  onNext: () => void
}

export function StatusBar({ loading, visibleFrom, visibleTo, total, selectedCount, canPrev, canNext, onPrev, onNext }: Props) {
  return (
    <footer className="statusBar">
      <span>{loading ? 'Загрузка...' : `Строки ${visibleFrom}-${visibleTo} из ${total}`}</span>
      <span>Выбрано: {selectedCount}</span>
      <span>API: localhost:8000</span>
      <div className="pager">
        <button disabled={!canPrev || loading} onClick={onPrev}>Назад</button>
        <button disabled={!canNext || loading} onClick={onNext}>Вперед</button>
      </div>
    </footer>
  )
}
