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
          <th>Характеристика</th>
          <th>Организация</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <td>{dateTimeRu(row.posting_at) || '—'}</td>
            <td>
              <strong>{row.warehouse_name}</strong>
              <span>{row.warehouse_ref1c}</span>
            </td>
            <td className="numCell">{signedQty(row.qty)}</td>
            <td className="numCell">{qty(row.qty_after)}</td>
            <td>
              <strong>{movementKindLabel[row.movement_kind] || row.movement_kind}</strong>
              <span>{recordTypeTone[row.record_type] ? <span className={`pill ${recordTypeTone[row.record_type]}`}>{row.record_type}</span> : row.record_type}</span>
            </td>
            <td>
              <code>{row.recorder_type}</code>
              <span>{row.recorder_ref}</span>
            </td>
            <td>{row.line_no || '—'}</td>
            <td>{row.ingest_source}</td>
            <td>{row.characteristic_ref || '—'}</td>
            <td>{row.organization_ref || '—'}</td>
          </tr>
        ))}
        {!rows.length && (
          <tr>
            <td colSpan={10}>
              <div className="emptyDetail">Движений нет</div>
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}
