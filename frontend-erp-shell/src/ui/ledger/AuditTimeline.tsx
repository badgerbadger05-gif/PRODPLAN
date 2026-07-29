import { dateTimeRu } from '../../lib/format'
import type { AuditEventView } from './types'

export function AuditTimeline({ events }: { events: readonly AuditEventView[] }) {
  return (
    <ol className="ledgerTimeline auditTimeline" aria-label="Аудит изменений">
      {events.map((event) => (
        <li key={event.id} className="ledgerTimelineStep audit">
          <div>
            <strong>{event.action}</strong>
            <span>{dateTimeRu(event.occurredAt) || '—'} · {event.actor}</span>
          </div>
          <p>Источник: {event.source}</p>
          {!!event.changes.length && (
            <dl className="auditDiff">
              {event.changes.map((change) => (
                <div key={change.field}>
                  <dt>{change.field}</dt>
                  <dd>
                    <del>{change.before ?? '—'}</del>
                    <span aria-hidden="true"> → </span>
                    <ins>{change.after ?? '—'}</ins>
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {event.correlationId && <code>{event.correlationId}</code>}
        </li>
      ))}
      {!events.length && <li className="emptyDetail">Событий аудита нет</li>}
    </ol>
  )
}
