import { coverageLabels, productionStatusOptions, productionStatusSelectValue, type OrderRow } from '../../../domain/productionControl'
import { dateRu, qty } from '../../../lib/format'
import { sortGlyph, tableColumnStyle, tableMinWidth, type TableSortState } from '../../tableDoctype'
import { productionOrderColumns, type ProductionOrderSortKey } from './productionOrdersDoctype'
import { ForecastShift } from '../period-plan/ForecastShift'
import { productionRowId } from './model'

type Props = {
  rows: OrderRow[]
  activeRow: OrderRow | null
  selectedIds: Set<number>
  sort: { sortBy: ProductionOrderSortKey | null; sortDir: 'asc' | 'desc' }
  onSelectIds: (ids: Set<number>) => void
  onActivate: (id: number) => void
  onOpenMaterials: (row: OrderRow) => void
  onChangeStatus: (row: OrderRow, status: string) => void
  onToggleSort: (key: ProductionOrderSortKey) => void
}

const manualProductionStatusOptions = productionStatusOptions.filter(
  ([value]) => value !== 'completed' && value !== 'not_created',
)

function orderSubline(row: OrderRow) {
  if (row.product_id == null) return 'Расчёт MRP · заказ ещё не создан'
  if (row.order_ref1c) return row.order_one_c_number || (row.order_source === '1c' ? row.order_number : 'Открыт в 1С')
  return `${dateRu(row.order_date)} · стр. ${row.line_number || '—'}`
}

function orderMainLine(row: OrderRow) {
  return row.order_prodplan_number || row.order_number
}

export function ProductionOrdersTable({ rows, activeRow, selectedIds, sort, onSelectIds, onActivate, onOpenMaterials, onChangeStatus, onToggleSort }: Props) {
  return (
    <table aria-label="Заказы на производство" className="journalTable productionOrdersTable" style={{ minWidth: tableMinWidth(productionOrderColumns) }}>
      <colgroup>
        {productionOrderColumns.map((column) => (
          <col key={column.key} className={column.grow ? 'growCol' : undefined} style={tableColumnStyle(column)} />
        ))}
      </colgroup>
      <thead>
        <tr>
          {productionOrderColumns.map((column) => (
            <th
              key={column.key}
              className={column.className}
              style={tableColumnStyle(column)}
              aria-sort={column.sortable && sort.sortBy === column.key ? (sort.sortDir === 'asc' ? 'ascending' : 'descending') : undefined}
            >
              {column.sortable ? (
                <button type="button" className="tableSortButton" onClick={() => onToggleSort(column.key as ProductionOrderSortKey)}>
                  {column.title}{sort.sortBy === column.key
                    ? sortGlyph(sort as TableSortState<ProductionOrderSortKey>, column.key as ProductionOrderSortKey)
                    : ''}
                </button>
              ) : column.title}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const rowId = productionRowId(row)
          const isProposal = row.product_id == null
          return (
          <tr
            key={row.journal_row_key || rowId}
            className={rowId === (activeRow ? productionRowId(activeRow) : null) ? 'activeRow' : ''}
            tabIndex={0}
            aria-selected={rowId === (activeRow ? productionRowId(activeRow) : null)}
            onClick={() => onActivate(rowId)}
            onDoubleClick={() => { if (!isProposal) onOpenMaterials(row) }}
            onKeyDown={(event) => {
              if (event.target !== event.currentTarget) return
              if (event.key === 'Enter') {
                event.preventDefault()
                onActivate(rowId)
                if (!isProposal) onOpenMaterials(row)
              } else if (event.key === ' ') {
                event.preventDefault()
                const next = new Set(selectedIds)
                if (next.has(rowId)) next.delete(rowId)
                else next.add(rowId)
                onSelectIds(next)
              }
            }}
          >
            <td className="checkCol">
              <input
                type="checkbox"
                aria-label={`Выбрать заказ ${orderMainLine(row)}`}
                checked={selectedIds.has(rowId)}
                onChange={(e) => {
                  const next = new Set(selectedIds)
                  if (e.target.checked) next.add(rowId)
                  else next.delete(rowId)
                  onSelectIds(next)
                }}
                onClick={(e) => e.stopPropagation()}
              />
            </td>
            <td className={`orderCell ${row.order_ref1c ? 'oneCOrderCell' : ''}`}>
              <strong title={orderMainLine(row)}>{orderMainLine(row)}</strong>
              <span title={orderSubline(row)}>{orderSubline(row)}</span>
            </td>
            <td
              className={`itemCell ${row.route_sheet_printed_at ? 'printedRouteSheetCell' : ''}`}
              title={row.route_sheet_printed_at ? `Маршрутный лист печатался ${dateRu(row.route_sheet_printed_at)}` : undefined}
            >
              <strong title={row.item_name}>
                {row.item_name}
                {row.paint_weld_chain && (
                  <span
                    className="muted"
                    title={row.paint_weld_chain.role === 'painted'
                      ? 'Цепочка окраска↔сварка: окрасочный (родительский) заказ'
                      : 'Цепочка окраска↔сварка: сварочный заказ (на основании окрасочного)'}
                  >
                    {row.paint_weld_chain.role === 'painted' ? ' ⛓🎨' : ' ⛓⚙'}
                  </span>
                )}
              </strong>
              <span title={row.item_article || row.item_code || ''}>
                {row.item_article || row.item_code || ''}
                {row.source === 'mrp' && <span className="planningBadge mrp">MRP</span>}
              </span>
            </td>
            <td className="numCell">
              <strong>{qty(row.remaining_qty)}</strong>
              <span>/ {qty(row.quantity)} {row.unit || ''}</span>
            </td>
            <td className="dateCell">
              <span>
                {row.source === 'mrp' ? 'Нужно' : 'С'}: {dateRu(row.planned_start_date) || '—'}
              </span>
              <span>По: {dateRu(row.planned_finish_date) || '—'}</span>
              <ForecastShift forecast={row} />
            </td>
            <td>
              <strong>{row.workshop_name || 'Не назначен'}</strong>
              <span className="muted">{row.stage_name || ''}</span>
            </td>
            <td>
              {isProposal ? (
                <span className="muted">Не создан</span>
              ) : (
                <select aria-label={`Статус заказа ${orderMainLine(row)}`} value={productionStatusSelectValue(row.status)} onChange={(e) => onChangeStatus(row, e.target.value)} onClick={(e) => e.stopPropagation()}>
                  {manualProductionStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              )}
            </td>
            <td>
              <span className={`pill ${row.coverage_status || row.status || 'unknown'}`}>
                {row.coverage_status === 'unknown'
                  ? coverageLabels.unknown
                  : row.coverage_label || coverageLabels[String(row.coverage_status || '')] || row.coverage_status || '—'}
              </span>
              {!!row.issue_count && <span className="muted issueCount">док. {row.issue_count}</span>}
            </td>
          </tr>
          )
        })}
      </tbody>
    </table>
  )
}
