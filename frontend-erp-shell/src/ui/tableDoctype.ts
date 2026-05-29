import type { CSSProperties } from 'react'

export type TableColumnDoctype = {
  key: string
  title: string
  className?: string
  width?: number
  minWidth?: number
  autoWidth?: boolean
  grow?: boolean
  align?: 'left' | 'right' | 'center'
  sortable?: boolean
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
