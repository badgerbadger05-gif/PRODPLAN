import type { OrderRow } from '../../../domain/productionControl'
import { productionRowId } from './model'

type Props = {
  rows: OrderRow[]
  selectedIds: Set<number>
  loading: boolean
  onExportTo1C: () => void
  onSyncFrom1C: () => void
  onProduce: () => void
  onClose: () => void
  onPiecework: () => void
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
  onClose,
  onPiecework,
  onPrintSelected,
  onDeleteSelected,
  onOpenSettings,
  onRefresh,
  onSelectAll,
  onClearSelection,
  rootProductLabel,
  onOpenRootProductFilter,
}: Props) {
  const selectedRows = rows.filter((row) => selectedIds.has(productionRowId(row)))
  const selectedOrders = selectedRows.filter((row) => row.product_id != null)
  const canProduce = selectedOrders.length === 1 && selectedRows.length === 1
  const canClose = canProduce
    && selectedRows[0]?.available_actions?.includes('close_1c')
    && Boolean(selectedRows[0]?.current_identity && selectedRows[0]?.source_revision)
  return (
    <div className="commandBar">
      <button className="primary" onClick={onExportTo1C} disabled={!selectedIds.size || loading} title="Создать и оперативно провести заказ на производство, затем создать непроведённое перемещение">Запустить в 1С</button>
      <button className="success" onClick={onProduce} disabled={!canProduce || loading} title="Указать фактическое количество, оформить выпуск и сдельный; факт будет принят после read-back Ledger">Произвести</button>
      <button onClick={onClose} disabled={!canClose || loading} title="Явно завершить выбранный заказ в 1С">Закрыть в 1С</button>
      <button onClick={onPiecework} disabled={!canProduce || loading}
        style={{ background: '#e8c7d6', borderColor: '#bc91a5', color: '#111', fontWeight: 700 }}
        title="Оформить работу сварщика отдельно от производства и окраски">Сдельный наряд</button>
      <button onClick={onSyncFrom1C} disabled={loading} title="Проверить статусы в 1С">Синхронизировать</button>
      <div className="barSeparator" />
      <button onClick={onPrintSelected} disabled={!selectedOrders.length}>Печать маршрутных</button>
      <button onClick={onDeleteSelected} disabled={!selectedOrders.some((row) => !row.order_ref1c) || loading} title="Backend проверит, что весь локальный заказ и его документы ещё не связаны с 1С">Удалить</button>
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
