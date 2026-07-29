import type {
  PlanningComparisonBatchDetail,
  PlanningComparisonBatchList,
} from '../domain/planningComparison'
import { api } from '../lib/api'

export function listPlanningComparisonBatches(limit = 50, offset = 0, signal?: AbortSignal) {
  return api<PlanningComparisonBatchList>(
    `/v1/planning-comparison/batches?limit=${limit}&offset=${offset}`,
    undefined,
    signal,
  )
}

export function getPlanningComparisonBatch(batchId: number, signal?: AbortSignal) {
  return api<PlanningComparisonBatchDetail>(
    `/v1/planning-comparison/batches/${batchId}`,
    undefined,
    signal,
  )
}

export function capturePlanningComparison(maxSkewSeconds = 300) {
  return api<PlanningComparisonBatchDetail>('/v1/planning-comparison/captures', {
    method: 'POST',
    body: JSON.stringify({ max_skew_seconds: maxSkewSeconds }),
  })
}
