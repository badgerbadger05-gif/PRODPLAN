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
              <th key={column.key} className={column.className} style={tableColumnStyle(column)} title={column.tooltip}>
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
          {state.rows.map((row) => {
            const id = idOf(row)
            const active = state.activeRow ? idOf(state.activeRow) === id : false
            return (
              <tr
                key={id}
                className={`${active ? 'activeRow' : ''} ${doctype.rowClassName?.(row) ?? ''}`.trim()}
                onClick={() => state.setActiveId(id)}
                onDoubleClick={() => onRowDoubleClick?.(row)}
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
