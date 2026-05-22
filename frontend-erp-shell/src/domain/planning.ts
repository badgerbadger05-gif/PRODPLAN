export type PlanningRunRow = {
  run_id: number
  status: string
  started_at: string | null
  finished_at: string | null
  horizon_days?: number | null
  order_count?: number | null
  purchase_count?: number | null
  overload_buckets?: number | null
}

export type PlanningRunsResponse = {
  rows: PlanningRunRow[]
  total: number
  limit: number
  offset: number
}

export type StartPlanningRunResponse = {
  status: string
  run_id: number
}

export type MrpSummary = {
  run: PlanningRunRow
  counts?: {
    production_orders?: number
    purchase_requests?: number
    rework_requests?: number
  }
  capacity?: {
    overloaded_buckets?: number
    overload_total?: number
    hours_planned_total?: number
    hours_available_total?: number
  }
  warnings?: Array<Record<string, unknown>>
}

export type MrpProductionRow = {
  order_id: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  need_date?: string | null
  start_date?: string | null
  finish_date?: string | null
  main_area_name?: string | null
  norm_hours_total?: number | null
  badge?: string | null
  turning_blank_priority?: boolean
  source_order_ids?: number[]
}

export type MrpPurchaseRow = {
  purchase_id: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  badge?: string | null
  late_supplier_order?: boolean
  turning_blank_priority?: boolean
  source_purchase_ids?: number[]
}

export type MrpReworkRow = {
  rework_id: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  requested_qty: number
  planned_qty: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  spec_code?: string | null
  spec_name?: string | null
  component_limit?: number | null
  component_blocked?: boolean
  component_partial?: boolean
  badge?: string | null
}

export type MrpCapacityRow = {
  area_id: number
  bucket_date?: string | null
  hours_planned: number
  hours_available: number
  overload_hours: number
}

export type MrpPagedResponse<T> = {
  rows: T[]
  total: number
  total_qty?: number
  limit: number
  offset: number
}

export function planningStatusLabel(status: string) {
  const value = status.toUpperCase()
  if (value === 'SUCCESS') return 'Успешно'
  if (value === 'RUNNING') return 'Выполняется'
  if (value === 'FAILED') return 'Ошибка'
  return status || 'Неизвестно'
}
