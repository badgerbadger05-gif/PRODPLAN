import { dateTimeRu } from '../../lib/format'
import type { ProvenanceStepView } from './types'

export function ProvenanceTimeline({ steps }: { steps: ProvenanceStepView[] }) {
  return (
    <ol className="ledgerTimeline" aria-label="Происхождение расчёта">
      {steps.map((step) => (
        <li key={step.id} className={`ledgerTimelineStep ${step.kind}`}>
          <div>
            <strong>{step.title}</strong>
            <span>{dateTimeRu(step.occurredAt) || '—'}{step.actor ? ` · ${step.actor}` : ''}</span>
          </div>
          {step.detail && <p>{step.detail}</p>}
          {step.correlationId && <code>{step.correlationId}</code>}
        </li>
      ))}
      {!steps.length && <li className="emptyDetail">Цепочка происхождения не загружена</li>}
    </ol>
  )
}

