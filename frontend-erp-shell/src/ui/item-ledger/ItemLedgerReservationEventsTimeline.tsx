import { dateTimeRu, qty } from '../../lib/format'
import type { ItemLedgerReservationEventsResponse } from '../../domain/itemLedger'

type EventRow = ItemLedgerReservationEventsResponse['rows'][number]

type Props = {
  rows: EventRow[]
  highlightedEventId?: number | null
}

const eventLabel: Record<string, string> = {
  open: 'Открыт',
  amend: 'Изменён',
  realize: 'Погашен',
  unrealize: 'Отменено погашение',
  cancel: 'Закрыт',
  release: 'Передан',
  carry: 'Перенесён',
  close: 'Закрыт',
  reopen: 'Возобновлён',
}

export function ItemLedgerReservationEventsTimeline({ rows, highlightedEventId = null }: Props) {
  return (
    <ol className="ledgerTimeline" aria-label="Журнал событий резерва">
      {rows.map((event) => (
        <li
          key={event.id}
          className={`ledgerTimelineStep ${event.event_kind}${highlightedEventId === event.id ? ' selected' : ''}`}
          aria-current={highlightedEventId === event.id ? 'step' : undefined}
        >
          <div>
            <strong>{eventLabel[event.event_kind] || event.event_kind}</strong>
            <span>{dateTimeRu(event.event_at) || '—'}</span>
          </div>
          <p>
            <strong>Резерв:</strong> {event.reserved_delta > 0 ? '+' : ''}{qty(event.reserved_delta)}
            {' / '}
            <strong>Факт:</strong> {event.realized_delta > 0 ? '+' : ''}{qty(event.realized_delta)}
            {' ('}
            {event.match_rule}
            {')'}
          </p>
          <p>
            {event.sle_id && <code>SLE #{event.sle_id}</code>}
            {event.fact_ref && (
              <>
                {' '}
                {event.fact_ref && <code>Факт {event.fact_ref}</code>}
                {event.fact_line_ref && <code> · строка {event.fact_line_ref}</code>}
              </>
            )}
          </p>
          <p>
            {event.cycle_id && (
              <>
                <strong>Цикл:</strong> <code>{event.cycle_id}</code>
              </>
            )}
            {!event.cycle_id && <span>Связь цикла не указана</span>}
          </p>
        </li>
      ))}
      {!rows.length && <li className="emptyDetail">События резерва не найдены</li>}
    </ol>
  )
}
