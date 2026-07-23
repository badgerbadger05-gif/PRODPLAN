import { StatusBadge } from '../kit'
import { dateTimeRu, qty } from '../../lib/format'
import type { ItemLedgerDriftResponse } from '../../domain/itemLedger'

type DriftRow = ItemLedgerDriftResponse['rows'][number]

type Props = {
  rows: DriftRow[]
}

const driftKindLabel: Record<string, string> = {
  shortfall: 'Недобор',
  surplus: 'Избыток',
  evaporation: 'Испарение',
}

const driftTone: Record<string, string> = {
  shortfall: 'shortage',
  surplus: 'success',
  evaporation: 'warning',
}

function formatNullableQty(value: number | null | undefined) {
  if (value === null || value === undefined) return '—'
  return qty(value)
}

function formatNullableText(value: unknown) {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

export function ItemLedgerDriftTable({ rows }: Props) {
  return (
    <table className="journalTable" aria-label="Таблица дрейфа номенклатуры">
      <thead>
        <tr>
          <th>ID</th>
          <th>Цикл</th>
          <th>Тип</th>
          <th className="numCell">Дрейф</th>
          <th className="numCell">Ожидалось</th>
          <th className="numCell">Факт</th>
          <th>Время</th>
          <th>Причина</th>
          <th>SLE корректировки</th>
          <th>Зрелый</th>
          <th>Цикл первого наблюдения</th>
          <th>Требование</th>
          <th>Детали</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <td>{row.id}</td>
            <td>{formatNullableText(row.cycle_id)}</td>
            <td>
              <StatusBadge tone={driftTone[row.kind] || ''}>{driftKindLabel[row.kind] || row.kind}</StatusBadge>
            </td>
            <td className="numCell">
              <strong>{row.drift_qty > 0 ? '+' : ''}{qty(row.drift_qty)}</strong>
            </td>
            <td className="numCell">{formatNullableQty(row.expected_stock)}</td>
            <td className="numCell">{formatNullableQty(row.actual_stock)}</td>
            <td>{dateTimeRu(row.at) || '—'}</td>
            <td>{formatNullableText(row.cause)}</td>
            <td className="numCell">{formatNullableText(row.adjustment_sle_id)}</td>
            <td>{row.matured ? 'Да' : 'Нет'}</td>
            <td>{formatNullableText(row.first_seen_cycle_id)}</td>
            <td>{formatNullableText(row.requirement_id)}</td>
            <td>
              {row.details ? (
                <code><pre>{JSON.stringify(row.details, null, 2)}</pre></code>
              ) : (
                '—'
              )}
            </td>
          </tr>
        ))}
        {!rows.length && (
          <tr>
            <td colSpan={13}>
              <div className="emptyDetail">Дрейфов нет</div>
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}
