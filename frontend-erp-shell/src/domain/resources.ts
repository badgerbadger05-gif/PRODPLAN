export type ProductionResource = {
  resource_id: number
  resource_name: string
  shift_offset?: number | null
  planning_range?: number | null
  capacity?: number | null
  work_schedule?: string | null
  daily_work_hours?: number | null
  buffer_days?: number | null
}

export type ProductionResourcePayload = {
  resource_name: string
  shift_offset?: number
  planning_range?: number
  capacity?: number
  work_schedule?: string
  daily_work_hours?: number
  buffer_days?: number
}

export type ResourceProductionKind = {
  id: number
  resource_id: number
  production_kind_id: number
  production_kind_name?: string | null
}

export type ProductionKind = {
  id: number
  ref_1c?: string | null
  name: string
}
