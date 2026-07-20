import { columnValue } from './fieldFormat'
import { canViewField, canViewRecord } from './permissions'
import type { AccessSubject, Doctype } from './types'

function csvCell(
  value: unknown,
  delimiter: ',' | ';',
  quote: 'minimal' | 'all',
) {
  let text = value == null ? '' : String(value)
  if (/^[\t\r ]*[=+\-@]/.test(text)) text = `'${text}`
  return quote === 'all' || text.includes(delimiter) || /["\r\n]/.test(text)
    ? `"${text.replaceAll('"', '""')}"`
    : text
}

export function buildDoctypeCsv<Row, Filters extends object, Detail>({
  doctype,
  rows,
  visibleColumns,
  access,
}: {
  doctype: Doctype<Row, Filters, Detail>
  rows: readonly Row[]
  visibleColumns: readonly string[]
  access: AccessSubject
}) {
  const config = typeof doctype.meta.exportCsv === 'object' ? doctype.meta.exportCsv : {}
  const delimiter = config.delimiter ?? ','
  const quote = config.quote ?? 'minimal'
  const lineEnding = config.lineEnding ?? '\r\n'
  const explicitColumns = config.columns
  const columns = explicitColumns
    ? explicitColumns
      .filter((column) => (
        (config.visibleColumnsOnly !== true || visibleColumns.includes(column.key))
        && canViewField(doctype.permissions, column.permissionField ?? column.key, access)
      ))
      .map((column) => ({ ...column, cellValue: column.value }))
    : doctype.columns
      .filter((column) => (
        column.type !== 'select-checkbox'
        && visibleColumns.includes(column.key)
        && canViewField(doctype.permissions, column.key, access)
      ))
      .map((column) => ({
        ...column,
        cellValue: (row: Row) => columnValue(column, row),
      }))
  const permittedRows = rows.filter((row) => canViewRecord(doctype.permissions, row, access))
  const lines = [
    columns.map((column) => csvCell(column.title, delimiter, quote)).join(delimiter),
    ...permittedRows.map((row) => columns.map((column) => csvCell(column.cellValue(row), delimiter, quote)).join(delimiter)),
  ]
  return `${lines.join(lineEnding)}${lineEnding}`
}

function safeFilename(value: string) {
  const cleaned = [...value]
    .map((character) => character.charCodeAt(0) < 32 || /[/\\<>:"|?*]/.test(character) ? '_' : character)
    .join('')
    .trim()
    .slice(0, 180)
  return cleaned || 'export.csv'
}

export function downloadCsv(csv: string, filename: string) {
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = safeFilename(filename)
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}
