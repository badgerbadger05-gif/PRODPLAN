import { dateTimeRu, qty } from '../../lib/format'
import type { LedgerPostingView } from './types'

type Props = {
  rows: LedgerPostingView[]
  activeId?: string | null
  onActivate?: (row: LedgerPostingView) => void
}

export function LedgerPostingTable({ rows, activeId, onActivate }: Props) {
  return (
    <table className="journalTable ledgerPostingTable">
      <thead>
        <tr>
          <th>Время</th>
          <th>Событие</th>
          <th>Номенклатура</th>
          <th>Пул</th>
          <th className="numCell">Изменение</th>
          <th>Документ-основание</th>
          <th>Связь</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.id}
            className={row.id === activeId ? 'activeRow' : ''}
            tabIndex={0}
            aria-selected={row.id === activeId}
            onClick={() => onActivate?.(row)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onActivate?.(row)
              }
            }}
          >
            <td>{dateTimeRu(row.occurredAt) || '—'}</td>
            <td>
              <strong>{row.eventType}</strong>
              {row.reversalOf && <span>сторно {row.reversalOf}</span>}
            </td>
            <td>{row.itemLabel}</td>
            <td><code>{row.poolKey}</code></td>
            <td className="numCell">
              <strong>{row.quantityDelta > 0 ? '+' : ''}{qty(row.quantityDelta)}</strong>
              <span>{row.unit || ''}</span>
            </td>
            <td>
              <strong>{row.sourceDocument}</strong>
              {row.sourceLine && <span>{row.sourceLine}</span>}
            </td>
            <td><code>{row.correlationId || '—'}</code></td>
          </tr>
        ))}
        {!rows.length && (
          <tr><td colSpan={7}><div className="emptyDetail">Проводок нет</div></td></tr>
        )}
      </tbody>
    </table>
  )
}

