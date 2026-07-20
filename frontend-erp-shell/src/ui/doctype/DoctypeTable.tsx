import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import { columnValue, formatField } from './fieldFormat'
import { sortGlyph, tableColumnStyle, tableMinWidth } from '../tableDoctype'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  onRowDoubleClick?: (row: Row) => void
}

export function DoctypeTable<Row, Filters extends object, Detail>({ doctype, state, onRowDoubleClick }: Props<Row, Filters, Detail>) {
  const idOf = (row: Row) => row[doctype.meta.idField] as string | number
  const activateRow = (index: number, tableRow: HTMLTableRowElement) => {
    const nextIndex = Math.max(0, Math.min(index, state.rows.length - 1))
    const next = state.rows[nextIndex]
    if (!next) return
    state.setActiveId(idOf(next))
    const body = tableRow.parentElement
    ;(body?.children[nextIndex] as HTMLTableRowElement | undefined)?.focus()
  }

  return (
    <div className="tablePane">
      <table className="journalTable" style={{ minWidth: tableMinWidth(doctype.columns) }}>
        <colgroup>
          {doctype.columns.map((column) => (
            <col key={column.key} style={tableColumnStyle(column)} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {doctype.columns.map((column) => (
              <th
                key={column.key}
                className={column.className}
                style={tableColumnStyle(column)}
                title={column.tooltip}
                aria-sort={column.sortable && state.sort?.sortBy === column.key
                  ? (state.sort.sortDir === 'asc' ? 'ascending' : 'descending')
                  : undefined}
              >
                {column.sortable ? (
                  <button className="tableSortButton" onClick={() => state.setSort(column.key)}>
                    {column.title}{state.sort ? sortGlyph(state.sort, column.key) : ''}
                  </button>
                ) : column.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {state.rows.map((row, rowIndex) => {
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
                    activateRow(state.rows.length - 1, event.currentTarget)
                  } else if (event.key === 'Enter') {
                    onRowDoubleClick?.(row)
                  }
                }}
              >
                {doctype.columns.map((column) => {
                  if (column.type === 'select-checkbox') {
                    const checked = doctype.meta.selectionMode === 'single'
                      ? active
                      : state.selectedIds.has(id)
                    return (
                      <td key={column.key} className={column.className} style={tableColumnStyle(column)}>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => {
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
