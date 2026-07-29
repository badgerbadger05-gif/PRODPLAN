import { StatusBadge } from '../kit'
import { qty } from '../../lib/format'
import type { ItemLedgerReservationsResponse } from '../../domain/itemLedger'

type ReservationRow = ItemLedgerReservationsResponse['rows'][number]

type Props = {
  rows: ReservationRow[]
  selectedReservationId?: number | null
  onSelect?: (row: ReservationRow) => void
}

const realizationLabel: Record<string, string> = {
  consume: 'Списание (consume)',
  make: 'Выпуск (make)',
}

const statusTone: Record<string, string> = {
  active: 'shortage',
  closed: 'success',
  released: 'to_move',
  carried: 'warning',
  cancelled: 'running',
}

export function ItemLedgerReservationsTable({ rows, selectedReservationId, onSelect }: Props) {
  const activateAt = (index: number, current: HTMLTableRowElement) => {
    const nextIndex = Math.max(0, Math.min(index, rows.length - 1))
    const next = rows[nextIndex]
    if (!next) return
    onSelect?.(next)
    ;(current.parentElement?.children[nextIndex] as HTMLTableRowElement | undefined)?.focus()
  }

  return (
    <table className="journalTable" aria-label="Резервы номенклатуры">
      <thead>
        <tr>
          <th>ID резерва</th>
          <th>План</th>
          <th>Заявка</th>
          <th>Период</th>
          <th>Тип</th>
          <th className="numCell">Резерв</th>
          <th className="numCell">Покрыто при фиксации</th>
          <th className="numCell">Пополнение</th>
          <th className="numCell">Получено</th>
          <th className="numCell">Осталось</th>
          <th>Статус</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => {
          const selected = selectedReservationId === row.reservation_id
          const period =
            row.priority.period_from || row.priority.period_to
              ? `${row.priority.period_from || '—'} — ${row.priority.period_to || '—'}`
              : '—'
          const modeLabel = realizationLabel[row.realization_mode] || row.realization_mode
          const tone = row.realization_mode === 'make' ? 'ready' : 'partial'
          return (
            <tr
              key={row.reservation_id}
              className={selected ? 'activeRow' : ''}
              tabIndex={selected ? 0 : -1}
              aria-label={`Резерв ${row.reservation_id}`}
              aria-selected={selected}
              onClick={() => onSelect?.(row)}
              onKeyDown={(event) => {
                if (!onSelect) return
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  onSelect?.(row)
                } else if (event.key === 'ArrowDown') {
                  event.preventDefault()
                  activateAt(index + 1, event.currentTarget)
                } else if (event.key === 'ArrowUp') {
                  event.preventDefault()
                  activateAt(index - 1, event.currentTarget)
                } else if (event.key === 'Home') {
                  event.preventDefault()
                  activateAt(0, event.currentTarget)
                } else if (event.key === 'End') {
                  event.preventDefault()
                  activateAt(rows.length - 1, event.currentTarget)
                }
              }}
            >
              <td>{row.reservation_id}</td>
              <td>{row.plan_name || '—'}</td>
              <td>{row.requirement_id}</td>
              <td>{period}</td>
              <td>
                <StatusBadge tone={tone}>{modeLabel}</StatusBadge>
              </td>
              <td className="numCell">{qty(row.reserved_qty)}</td>
              <td className="numCell">{qty(row.covered_from_stock_at_freeze_qty)}</td>
              <td className="numCell">{qty(row.replenishment_required_qty)}</td>
              <td className="numCell">{qty(row.replenishment_received_qty)}</td>
              <td className="numCell"><strong>{qty(row.replenishment_remaining_qty)}</strong></td>
              <td><StatusBadge tone={statusTone[row.lifecycle_status] || ''}>{row.lifecycle_status}</StatusBadge></td>
            </tr>
          )
        })}
        {!rows.length && (
          <tr>
            <td colSpan={11}>
              <div className="emptyDetail">Резервов нет</div>
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}
