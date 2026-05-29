import type { OrderRow } from '../../../domain/productionControl'

type Props = {
  rows: OrderRow[]
  selectedIds: Set<number>
  loading: boolean
  onExportTo1C: () => void
  onSyncFrom1C: () => void
  onProduce: () => void
  onPrintSelected: () => void
  onCreateMaterialIssues: () => void
  onLoadMaterials: () => void
  onOpenSettings: () => void
  onRefresh: () => void
  onSelectAll: () => void
  onClearSelection: () => void
}

export function ProductionCommandBar({
  rows,
  selectedIds,
  loading,
  onExportTo1C,
  onSyncFrom1C,
  onProduce,
  onPrintSelected,
  onCreateMaterialIssues,
  onLoadMaterials,
  onOpenSettings,
  onRefresh,
  onSelectAll,
  onClearSelection,
}: Props) {
  const selectedRows = rows.filter((row) => selectedIds.has(row.product_id))
  const selectedProduceRow = selectedRows.length === 1 ? selectedRows[0] : null
  const canProduce = Boolean(
    selectedProduceRow
    && Number(selectedProduceRow.remaining_qty ?? 0) > 0
    && (selectedProduceRow.coverage_status === 'assembled' || selectedProduceRow.issue_status === 'posted'),
  )
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
      <button onClick={onSyncFrom1C} disabled={loading} title="Проверить статусы в 1С">Синхронизировать</button>
      <div className="barSeparator" />
      <button onClick={onPrintSelected} disabled={!selectedIds.size}>Печать маршрутных</button>
      <button onClick={onCreateMaterialIssues} disabled={!selectedIds.size || loading}>Выдача материалов</button>
      <button onClick={onLoadMaterials} disabled={!rows.length}>Материалы</button>
      <button onClick={onOpenSettings}>Настройки</button>
      <button onClick={onRefresh} disabled={loading}>Обновить</button>
      <div className="barSeparator" />
      <button onClick={onSelectAll} disabled={!rows.length}>Выбрать все</button>
      <button onClick={onClearSelection} disabled={!selectedIds.size}>Снять выбор</button>
    </div>
  )
}
