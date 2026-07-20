import type { OrderRow } from '../../../domain/productionControl'

type Props = {
  rows: OrderRow[]
  selectedIds: Set<number>
  loading: boolean
  onExportTo1C: () => void
  onSyncFrom1C: () => void
  onProduce: () => void
  onCloseChain: () => void
  onPrintSelected: () => void
  onDeleteSelected: () => void
  onOpenSettings: () => void
  onRefresh: () => void
  onSelectAll: () => void
  onClearSelection: () => void
  rootProductLabel: string
  onOpenRootProductFilter: () => void
}

export function ProductionCommandBar({
  rows,
  selectedIds,
  loading,
  onExportTo1C,
  onSyncFrom1C,
  onProduce,
  onCloseChain,
  onPrintSelected,
  onDeleteSelected,
  onOpenSettings,
  onRefresh,
  onSelectAll,
  onClearSelection,
  rootProductLabel,
  onOpenRootProductFilter,
}: Props) {
  const selectedRows = rows.filter((row) => selectedIds.has(row.product_id))
  const selectedProduceRow = selectedRows.length === 1 ? selectedRows[0] : null
  const canProduce = Boolean(
    selectedProduceRow
    && Number(selectedProduceRow.remaining_qty ?? 0) > 0
    && (selectedProduceRow.coverage_status === 'assembled' || selectedProduceRow.issue_status === 'posted'),
  )
  const canDeleteSelected = selectedRows.length > 0 && selectedRows.every((row) => !row.order_ref1c)
  const chainRow = selectedRows.length === 1 && selectedRows[0].paint_weld_chain ? selectedRows[0] : null
  const chainTitle = !selectedIds.size
    ? 'Выберите одну строку цепочки окраска↔сварка'
    : selectedIds.size > 1
      ? 'Для закрытия цепочки выберите только одну строку'
      : chainRow
        ? 'Выпуски обеих строк, один комбинированный сдельный, закрытие обоих заказов'
        : 'Выбранная строка не входит в цепочку окраска↔сварка'
  const produceTitle = !selectedIds.size
    ? 'Выберите одну строку чекбоксом'
    : selectedIds.size > 1
      ? 'Для выпуска выберите только одну строку'
      : canProduce
        ? 'Создать выпуск в 1С'
        : 'Сначала нужно проведённое перемещение материалов'
  return (
    <div className="commandBar">
      <button className="primary" onClick={onExportTo1C} disabled={!selectedIds.size || loading} title="Создать и оперативно провести заказ на производство, затем создать непроведённое перемещение">Запустить в 1С</button>
      <button className="success" onClick={onProduce} disabled={!canProduce || loading} title={produceTitle}>Произвести</button>
      <button className="success" onClick={onCloseChain} disabled={!chainRow || loading} title={chainTitle}>Закрыть цепочку</button>
      <button onClick={onSyncFrom1C} disabled={loading} title="Проверить статусы в 1С">Синхронизировать</button>
      <div className="barSeparator" />
      <button onClick={onPrintSelected} disabled={!selectedIds.size}>Печать маршрутных</button>
      <button onClick={onDeleteSelected} disabled={!canDeleteSelected || loading} title={canDeleteSelected ? 'Удалить локальные заказы, которые ещё не открыты в 1С' : 'Можно удалить только заказы без 1С'}>Удалить</button>
      <button onClick={onRefresh} disabled={loading}>Обновить</button>
      <div className="barSeparator" />
      <button onClick={onSelectAll} disabled={!rows.length}>Выбрать все</button>
      <button onClick={onClearSelection} disabled={!selectedIds.size}>Снять выбор</button>
      <div className="barSeparator" />
      <button onClick={onOpenRootProductFilter}>Корневое изделие</button>
      <span className="toolbarText">{rootProductLabel}</span>
      <div className="commandBarSpacer" />
      <button onClick={onOpenSettings}>Настройки</button>
    </div>
  )
}
