import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import { columnValue, formatField } from './fieldFormat'
import { sortGlyph, tableColumnStyle, tableMinWidth } from '../tableDoctype'
import type { AccessSubject } from './permissions'
import { canViewField, canViewRecord } from './permissions'

function BulkSelectionCheckbox({
  checked,
  indeterminate,
  disabled,
  onChange,
}: {
  checked: boolean
  indeterminate: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
      aria-label="Выбрать все видимые строки"
    />
  )
}

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  onRowDoubleClick?: (row: Row) => void
  visibleColumns?: readonly string[]
  density?: 'compact' | 'comfortable'
  access: AccessSubject
}

export function DoctypeTable<Row, Filters extends object, Detail>({
  doctype,
  state,
  onRowDoubleClick,
  visibleColumns,
  density = 'compact',
  access,
}: Props<Row, Filters, Detail>) {
  const idOf = (row: Row) => row[doctype.meta.idField] as string | number
  const columns = visibleColumns
    ? doctype.columns.filter((column) => visibleColumns.includes(column.key) && canViewField(doctype.permissions, column.key, access))
    : doctype.columns.filter((column) => canViewField(doctype.permissions, column.key, access))
  const rows = state.rows.filter((row) => canViewRecord(doctype.permissions, row, access))
  const visibleIds = rows
    .filter((row) => doctype.selectable?.(row) !== false)
    .map(idOf)
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => state.selectedIds.has(id))
  const someVisibleSelected = visibleIds.some((id) => state.selectedIds.has(id))
  const activateRow = (index: number, tableRow: HTMLTableRowElement) => {
    const nextIndex = Math.max(0, Math.min(index, rows.length - 1))
    const next = rows[nextIndex]
    if (!next) return
    state.setActiveId(idOf(next))
    const body = tableRow.parentElement
    ;(body?.children[nextIndex] as HTMLTableRowElement | undefined)?.focus()
  }

  return (
    <div className={`tablePane doctypeTable--${density}`}>
      <table className="journalTable" style={{ minWidth: tableMinWidth(columns) }}>
        <colgroup>
          {columns.map((column) => (
            <col key={column.key} style={tableColumnStyle(column)} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                className={column.className}
                style={tableColumnStyle(column)}
                title={column.tooltip}
                aria-sort={column.sortable && state.sort?.sortBy === column.key
                  ? (state.sort.sortDir === 'asc' ? 'ascending' : 'descending')
                  : undefined}
              >
                {column.type === 'select-checkbox' && doctype.meta.selectionMode === 'multiple' ? (
                  <BulkSelectionCheckbox
                    checked={allVisibleSelected}
                    indeterminate={!allVisibleSelected && someVisibleSelected}
                    disabled={!visibleIds.length}
                    onChange={state.setVisibleSelection}
                  />
                ) : column.sortable ? (
                  <button className="tableSortButton" onClick={() => state.setSort(column.key)}>
                    {column.title}{state.sort ? sortGlyph(state.sort, column.key) : ''}
                  </button>
                ) : column.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => {
            const id = idOf(row)
            const active = state.activeRow ? idOf(state.activeRow) === id : false
            return (
              <tr
                key={id}
                className={`${active ? 'activeRow' : ''} ${doctype.rowClassName?.(row) ?? ''}`.trim()}
                onClick={() => state.setActiveId(id)}
                onDoubleClick={() => onRowDoubleClick?.(row)}
                aria-selected={active}
                tabIndex={active ? 0 : -1}
                onKeyDown={(event) => {
                  if (event.key === 'ArrowDown') {
                    event.preventDefault()
                    activateRow(rowIndex + 1, event.currentTarget)
                  } else if (event.key === 'ArrowUp') {
                    event.preventDefault()
                    activateRow(rowIndex - 1, event.currentTarget)
                  } else if (event.key === 'Home') {
                    event.preventDefault()
                    activateRow(0, event.currentTarget)
                  } else if (event.key === 'End') {
                    event.preventDefault()
                    activateRow(rows.length - 1, event.currentTarget)
                  } else if (event.key === 'Enter') {
                    event.preventDefault()
                    onRowDoubleClick?.(row)
                  }
                }}
              >
                {columns.map((column) => {
                  if (column.type === 'select-checkbox') {
                    const selectable = doctype.selectable?.(row) !== false
                    const checked = doctype.meta.selectionMode === 'single'
                      ? active
                      : state.selectedIds.has(id)
                    return (
                      <td key={column.key} className={column.className} style={tableColumnStyle(column)}>
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={!selectable}
                          title={!selectable
                            ? doctype.selectionDisabledReason?.(row) ?? 'Строка недоступна для выбора'
                            : undefined}
                          onChange={() => {
                            if (!selectable) return
                            if (doctype.meta.selectionMode === 'single') state.setActiveId(id)
                            else state.toggleSelection(id)
                          }}
                          onClick={(event) => event.stopPropagation()}
                          aria-label={`Выбрать строку ${id}`}
                        />
                      </td>
                    )
                  }
                  const raw = columnValue(column, row)
                  const option = column.options?.[String(raw ?? '')]
                  return (
                    <td key={column.key} className={column.className} style={tableColumnStyle(column)}>
                      {column.render
                        ? column.render(row)
                        : column.type === 'status'
                          ? <span className={`pill ${option?.tone ?? ''}`}>{formatField(raw, column.type, column.options)}</span>
                          : formatField(raw, column.type, column.options)}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
import { useEffect, useRef } from 'react'
