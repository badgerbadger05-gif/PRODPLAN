import { dateTimeRu, qty } from '../../lib/format'
import type { ItemLedgerMovementRow } from '../../domain/itemLedger'

type Props = {
  rows: ItemLedgerMovementRow[]
}

const movementKindLabel: Record<string, string> = {
  assembly_in: 'Сборка (вход)',
  assembly_out: 'Сборка (выход)',
  transfer_in: 'Перемещение (вход)',
  transfer_out: 'Перемещение (выход)',
  receipt: 'Приход',
  writeoff: 'Списание',
  reconcile_adjustment: 'Сверка',
  seed: 'Начальный остаток',
}

const recordTypeTone: Record<string, string> = {
  Receipt: 'success',
  Expense: 'shortage',
}

const recordTypeLabel: Record<string, string> = {
  Receipt: 'Приход',
  Expense: 'Расход',
}

const ingestSourceLabel: Record<string, string> = {
  document_pull: 'Документы 1С',
  balance_reconcile: 'Сверка остатков',
  seed: 'Начальный остаток',
  adjustment: 'Корректировка',
}

function recorderTypeLabel(value: string) {
  const labels: Record<string, string> = {
    Document_СборкаЗапасов: 'Сборка запасов',
    Document_ПеремещениеЗапасов: 'Перемещение запасов',
  }
  return labels[value] || value.replace(/^Document_/, '').replaceAll('_', ' ') || 'Документ 1С'
}

function signedQty(value: number) {
  return <strong>{value > 0 ? '+' : ''}{qty(value)}</strong>
}

export function ItemLedgerMovementsTable({ rows }: Props) {
  return (
    <table className="journalTable ledgerPostingTable" aria-label="Движения номенклатуры">
      <thead>
        <tr>
          <th>Время</th>
          <th>Склад</th>
          <th>Изменение</th>
          <th className="numCell">Остаток после</th>
          <th>Тип движения</th>
          <th>Документ</th>
          <th>Строка</th>
          <th>Источник</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <td>{dateTimeRu(row.posting_at) || '—'}</td>
            <td>
              <strong title={row.warehouse_ref1c}>{row.warehouse_name || 'Склад 1С'}</strong>
            </td>
            <td className="numCell">{signedQty(row.qty)}</td>
            <td className="numCell">{qty(row.qty_after)}</td>
            <td>
              <strong>{movementKindLabel[row.movement_kind] || row.movement_kind}</strong>
              <span>{recordTypeTone[row.record_type] ? <span className={`pill ${recordTypeTone[row.record_type]}`}>{recordTypeLabel[row.record_type]}</span> : row.record_type}</span>
            </td>
            <td title={`${row.recorder_type} ${row.recorder_ref}`}>
              <strong>{recorderTypeLabel(row.recorder_type)}</strong>
              <span>{row.recorder_number || (row.basis_order_number ? `Заказ ${row.basis_order_number}` : 'Документ 1С')}</span>
            </td>
            <td>{row.line_no || '—'}</td>
            <td>{ingestSourceLabel[row.ingest_source] || row.ingest_source || '—'}</td>
          </tr>
        ))}
        {!rows.length && (
          <tr>
            <td colSpan={8}>
              <div className="emptyDetail">Движений нет</div>
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}
