import type { CSSProperties, ReactNode } from 'react'

export type FieldType =
  | 'text'
  | 'number'
  | 'qty'
  | 'money'
  | 'date'
  | 'datetime'
  | 'enum'
  | 'ref'
  | 'status'
  | 'bool'
  | 'select-checkbox'

export type FieldTone = 'ok' | 'warn' | 'danger' | 'info' | 'muted'

export type FieldOption = {
  label: string
  tone?: FieldTone
}

export type TableColumnDoctype = {
  key: string
  title: string
  // Однострочное пояснение термина, показывается как title заголовка колонки
  tooltip?: string
  className?: string
  width?: number
  minWidth?: number
  autoWidth?: boolean
  grow?: boolean
  align?: 'left' | 'right' | 'center'
  sortable?: boolean
}

export type DoctypeColumn<Row, Filters extends object = Record<string, unknown>> = TableColumnDoctype & {
  type?: FieldType
  value?: (row: Row) => unknown
  options?: Record<string, FieldOption>
  render?: (row: Row) => ReactNode
  visible?: (context: {
    filters: Filters
    listMeta: Record<string, unknown>
  }) => boolean
}

export type TableSortState<TKey extends string = string> = {
  sortBy: TKey
  sortDir: 'asc' | 'desc'
}

export function tableColumnStyle(column: TableColumnDoctype): CSSProperties {
  return {
    width: column.grow ? undefined : column.autoWidth ? column.minWidth : column.width,
    minWidth: column.minWidth,
    textAlign: column.align,
  }
}

export function tableMinWidth(columns: TableColumnDoctype[]) {
  return columns.reduce((sum, column) => sum + (column.autoWidth ? column.minWidth ?? 64 : column.width ?? column.minWidth ?? 120), 0)
}

export function sortGlyph<TKey extends string>(state: TableSortState<TKey>, key: TKey) {
  if (state.sortBy !== key) return ''
  return state.sortDir === 'asc' ? ' ▲' : ' ▼'
}
