export type CutoffGrade = 'exact' | 'near' | 'invalid'
export type DiffClassification = 'equal' | 'changed' | 'stable_only' | 'shadow_only'

export type PlanningComparisonKindMetrics = {
  equal: number
  changed: number
  stable_only: number
  shadow_only: number
  absolute_delta: string
}

export type PlanningComparisonMetrics = {
  cutoff_grade?: CutoffGrade
  rows?: number
  by_kind?: Record<string, PlanningComparisonKindMetrics>
}

export type PlanningComparisonBatch = {
  id: number
  capture_key: string
  created_at: string
  cutoff_grade: CutoffGrade
  cutoff_reason?: string | null
  stable_run_key?: string | null
  shadow_run_key?: string | null
  metrics: PlanningComparisonMetrics
}

export type PlanningComparisonDiff = {
  result_kind: string
  canonical_key: string
  item_key: string
  stable_quantity: string
  shadow_quantity: string
  delta_quantity: string
  classification: DiffClassification
}

export type PlanningComparisonBatchDetail = PlanningComparisonBatch & {
  diffs: PlanningComparisonDiff[]
}

export type PlanningComparisonBatchList = {
  rows: PlanningComparisonBatch[]
  total: number
  limit: number
  offset: number
}
