import { columnValue } from './fieldFormat'
import { canViewField, canViewRecord } from './permissions'
import type { AccessSubject, Doctype } from './types'

function csvCell(value: unknown) {
  let text = value == null ? '' : String(value)
  if (/^[\t\r ]*[=+\-@]/.test(text)) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
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
  const columns = doctype.columns.filter((column) => (
    column.type !== 'select-checkbox'
    && visibleColumns.includes(column.key)
    && canViewField(doctype.permissions, column.key, access)
  ))
  const permittedRows = rows.filter((row) => canViewRecord(doctype.permissions, row, access))
  const lines = [
    columns.map((column) => csvCell(column.title)).join(','),
    ...permittedRows.map((row) => columns.map((column) => csvCell(columnValue(column, row))).join(',')),
  ]
  return `${lines.join('\r\n')}\r\n`
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
