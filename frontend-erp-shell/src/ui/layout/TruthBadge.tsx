import { dateTimeRu } from '../../lib/format'
import type { TruthBadgeMeta } from '../../services/planningTruth'

export function TruthBadge({ meta }: { meta: TruthBadgeMeta | null }) {
  const status = meta?.truth_status || null
  const neutral = status === null
  const accepted = status === 'accepted'
  const label = neutral
    ? 'Статус истины не получен'
    : accepted
    ? 'Истина принята'
    : status === 'stale'
      ? 'Истина устарела'
      : status === 'unavailable'
        ? 'Истина недоступна'
        : `Истина недоступна: ${status}`
  const generation = meta?.ledger_generation != null
    ? `Ledger ${meta.ledger_generation}`
    : 'Ledger —'
  const cutoff = meta?.cutoff ? dateTimeRu(meta.cutoff) : 'cutoff —'

  return (
    <div
      className={`truthBadge ${neutral ? 'neutral' : accepted ? 'accepted' : 'unavailable'}`}
      title={meta?.truth_reason || undefined}
      role="status"
    >
      {label} · {generation} · {cutoff}
    </div>
  )
}
