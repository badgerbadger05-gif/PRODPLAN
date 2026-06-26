// Типы ремонтного модуля спецификаций (операции A + превью смены вида).
// Зеркалят результаты backend/app/services/spec_repair.py.

export type PendingOneC = {
  specs: number[]
  note?: string
}

// Запросы (dry_run по умолчанию true на бэке; фронт всегда шлёт его явно).
export type RestageRequest = {
  component_id: number
  new_stage_id?: number | null
  dry_run: boolean
}

export type MoveRequest = {
  component_id: number
  target_spec_id: number
  new_stage_id?: number | null
  force?: boolean
  dry_run: boolean
}

export type AddRequest = {
  spec_id: number
  item_id: number
  quantity: number
  component_type?: string
  stage_id?: number | null
  component_spec_ref1c?: string | null
  dry_run: boolean
}

export type KindChangePreviewRequest = {
  item_id: number
  new_production_kind_id: number
}

// Результат записи в 1С (присутствует только при dry_run=false). Форма зависит от
// операции (changed/op/...), потому держим её мягко-типизированной.
export type Writeback1C = Record<string, unknown>

export type RestageResult = {
  action: 'restage'
  ok: boolean
  component_id: number
  spec_id: number
  old_stage_id: number | null
  new_stage_id: number | null
  warnings: unknown[]
  pending_1c: PendingOneC
  dry_run: boolean
  writeback_1c?: Writeback1C
}

export type MoveResult = {
  action: 'move'
  ok: boolean
  component_id: number
  item_id: number
  from_spec_id: number
  to_spec_id: number
  stage_id: number | null
  safety: {
    global_presence_after: number
    specs_before: number[]
    specs_after: number[]
  }
  warnings: unknown[]
  pending_1c: PendingOneC
  dry_run: boolean
  writeback_1c?: Writeback1C
}

export type AddWarning = {
  code: string
  specs?: number[]
  hint?: string
}

export type AddResult = {
  action: 'add'
  ok: boolean
  spec_id: number
  item_id: number
  stage_id: number | null
  component_type: string
  warnings: AddWarning[]
  pending_1c: PendingOneC
  dry_run: boolean
  writeback_1c?: Writeback1C
}

export type ProductionKindRef = {
  id: number
  ref_1c?: string | null
  name: string
} | null

export type KindChangePreviewResult = {
  action: 'kind_change_preview'
  item_id: number
  current_spec_id: number
  current_spec_ref1c: string | null
  current_kind: ProductionKindRef
  new_kind: ProductionKindRef
  cascade: {
    affected_parent_rows: number
    parents: Array<{
      parent_spec_id: number
      parent_spec_code: string | null
      component_id: number
      component_type: string | null
    }>
    note: string
  }
}

// Справочники для выпадающих списков.
export type StageOption = { value: number; label: string }
export type ProductionKind = { id: number; ref_1c?: string | null; name: string }
