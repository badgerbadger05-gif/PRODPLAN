import { ApiError } from '../lib/api'

export type TruthBadgeMeta = {
  truth_status?: string | null
  truth_reason?: string | null
  ledger_generation?: number | null
  cutoff?: string | null
}

export function truthBadgeMetaFromApiError(error: unknown): TruthBadgeMeta | null {
  if (!(error instanceof ApiError) || error.status !== 503) return null
  if (!error.detail || typeof error.detail !== 'object') return null
  const detail = error.detail as Record<string, unknown>
  if (detail.code !== 'planning_truth_unavailable') return null
  return {
    truth_status: typeof detail.truth_status === 'string'
      ? detail.truth_status
      : 'unavailable',
    truth_reason: typeof detail.reason === 'string' ? detail.reason : null,
    ledger_generation: typeof detail.ledger_generation === 'number'
      ? detail.ledger_generation
      : null,
    cutoff: typeof detail.cutoff === 'string' ? detail.cutoff : null,
  }
}
