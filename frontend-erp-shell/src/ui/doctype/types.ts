import type { ReactNode } from 'react'
import type { DoctypeColumn, FieldOption, FieldType } from '../tableDoctype'

export type RowId = string | number
export type Role = 'admin' | 'planner' | 'buyer' | 'shopfloor' | 'viewer' | (string & {})
export type Permission = string

export type ListParams<Filters> = {
  limit: number
  offset: number
  filters: Filters
  sortBy?: string
  sortDir?: 'asc' | 'desc'
}

export type ListResult<Row, Meta extends object = object> = {
  rows: Row[]
  total: number
  limit?: number
  offset?: number
} & Meta

export type FilterOption = {
  value: string
  label: string
}

export type FilterDef<Filters> =
  | {
      kind: 'search'
      field: keyof Filters
      placeholder?: string
      debounceMs?: number
    }
  | {
      kind: 'select'
      field: keyof Filters
      label: string
      options: FilterOption[]
      allowEmpty?: boolean
    }
  | {
      kind: 'dateRange'
      fieldFrom: keyof Filters
      fieldTo: keyof Filters
      label: string
    }
  | {
      kind: 'toggle'
      field: keyof Filters
      label: string
    }

export type DialogRequest = {
  dialog: string
  payload?: unknown
}

export type ActionResult = {
  message?: string
  error?: string
  reload?: boolean
  open?: DialogRequest
}

export type ActionContext<Row> = {
  rows: Row[]
  activeRow: Row | null
  selection: Row[]
}

export type ActionDef<Row> = {
  key: string
  label: string
  scope: 'global' | 'selection' | 'row'
  tone?: 'primary' | 'default' | 'danger'
  enabled?: (context: ActionContext<Row>) => boolean
  visible?: (context: ActionContext<Row>) => boolean
  confirm?: string
  run(context: ActionContext<Row>): Promise<ActionResult>
}

export type DetailField<T> = {
  key: keyof T
  label: string
  type?: FieldType
  options?: Record<string, FieldOption>
  span?: 1 | 2
}

export type DetailSection<T> = {
  title?: string
  fields?: Array<DetailField<T>>
  table?: {
    rows: (detail: T) => unknown[]
    columns: Array<DoctypeColumn<unknown>>
  }
}

export type DetailLayout<T> = {
  sections: Array<DetailSection<T>>
}

export type DoctypePermissions = {
  view?: Array<Role | Permission>
  actions?: Record<string, Role | Permission | Array<Role | Permission>>
}

export type Doctype<Row, Filters, Detail = never> = {
  meta: {
    name: string
    title: string
    subtitle: string
    hotkeys?: string
    idField: keyof Row
  }
  initialFilters: Filters
  dataSource: {
    list(params: ListParams<Filters>, signal?: AbortSignal): Promise<ListResult<Row>>
    detail?(id: RowId, signal?: AbortSignal): Promise<Detail>
  }
  columns: Array<DoctypeColumn<Row>>
  filters?: Array<FilterDef<Filters>>
  actions?: Array<ActionDef<Row>>
  detail?: DetailLayout<Detail | Row>
  permissions: DoctypePermissions
  renderExtraToolbar?: (context: ActionContext<Row>) => ReactNode
}

export type SortState = {
  sortBy: string
  sortDir: 'asc' | 'desc'
}

