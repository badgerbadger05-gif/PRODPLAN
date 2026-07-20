import type { ReactNode } from 'react'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import type { DoctypeColumn, FieldOption, FieldType } from '../tableDoctype'

function number(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  return Number(value).toLocaleString('ru-RU')
}

function money(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  return Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function option(value: unknown, options?: Record<string, FieldOption>) {
  const key = String(value ?? '')
  return options?.[key]?.label ?? (key || '—')
}

function reference(value: unknown) {
  if (value && typeof value === 'object') {
    const row = value as Record<string, unknown>
    return String(row.name ?? row.label ?? row.ref ?? '—')
  }
  return String(value ?? '—')
}

export function formatField(
  value: unknown,
  type: FieldType = 'text',
  options?: Record<string, FieldOption>,
): ReactNode {
  switch (type) {
    case 'number':
      return number(value)
    case 'qty':
      return value === null || value === undefined || value === '' ? '—' : qty(value)
    case 'money':
      return money(value)
    case 'date':
      return dateRu(value == null ? null : String(value)) || '—'
    case 'datetime':
      return dateTimeRu(value == null ? null : String(value)) || '—'
    case 'enum':
    case 'status':
      return option(value, options)
    case 'ref':
      return reference(value)
    case 'bool':
      return value ? '✓' : '—'
    case 'select-checkbox':
      return null
    default:
      return String(value ?? '—')
  }
}

export function columnValue<Row>(column: DoctypeColumn<Row>, row: Row) {
  if (column.value) return column.value(row)
  return (row as Record<string, unknown>)[column.key]
}
