import { coverageLabels, productionStatusOptions, productionStatusSelectValue, type OrderRow } from '../../../domain/productionControl'
import { dateRu, qty } from '../../../lib/format'
import { sortGlyph, tableColumnStyle, tableMinWidth, type TableSortState } from '../../tableDoctype'
import { productionOrderColumns, type ProductionOrderSortKey } from './productionOrdersDoctype'

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

const manualProductionStatusOptions = productionStatusOptions.filter(([value]) => value !== 'completed')

function ForecastShift({ row }: { row: OrderRow }) {
  if (row.forecast_shift_days === null || row.forecast_shift_days === undefined) return null
  const days = Number(row.forecast_shift_days)
  if (!Number.isFinite(days) || days === 0) return null
  const cls = days > 5 ? 'late' : days > 0 ? 'warn' : 'early'
  const label = `${days > 0 ? '+' : ''}${days} дн`
  const dateText = row.forecast_date ? dateRu(row.forecast_date).slice(0, 5) : ''
  const title = [row.forecast_reason, row.forecast_date ? `прогноз ${dateRu(row.forecast_date)}` : null].filter(Boolean).join(' · ')
  return <span className={`forecastShift ${cls}`} title={title}>{label}{dateText ? ` · ${dateText}` : ''}</span>
}

function orderSubline(row: OrderRow) {
  if (row.order_ref1c) return row.order_one_c_number || (row.order_source === '1c' ? row.order_number : 'Открыт в 1С')
  return `${dateRu(row.order_date)} · стр. ${row.line_number || '—'}`
}

function orderMainLine(row: OrderRow) {
  return row.order_prodplan_number || row.order_number
}

function planningZoneLabel(zone?: string | null) {
  if (zone === 'red') return 'Красная зона'
  if (zone === 'yellow') return 'Жёлтая зона'
  if (zone === 'green') return 'Зелёная зона'
  return zone || 'DBR'
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
        {rows.map((row) => (
          <tr
            key={row.product_id}
            className={row.product_id === activeRow?.product_id ? 'activeRow' : ''}
            tabIndex={0}
            aria-selected={row.product_id === activeRow?.product_id}
            onClick={() => onActivate(row.product_id)}
            onDoubleClick={() => onOpenMaterials(row)}
            onKeyDown={(event) => {
              if (event.target !== event.currentTarget) return
              if (event.key === 'Enter') {
                event.preventDefault()
                onActivate(row.product_id)
                onOpenMaterials(row)
              } else if (event.key === ' ') {
                event.preventDefault()
                const next = new Set(selectedIds)
                if (next.has(row.product_id)) next.delete(row.product_id)
                else next.add(row.product_id)
                onSelectIds(next)
              }
            }}
          >
            <td className="checkCol">
              <input
                type="checkbox"
                aria-label={`Выбрать заказ ${orderMainLine(row)}`}
                checked={selectedIds.has(row.product_id)}
                onChange={(e) => {
                  const next = new Set(selectedIds)
                  if (e.target.checked) next.add(row.product_id)
                  else next.delete(row.product_id)
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
                {row.planning?.contour === 'dbr_feeder' && (
                  <span
                    className={`planningBadge ${row.planning.zone || 'dbr'}`}
                    title={[
                      planningZoneLabel(row.planning.zone),
                      row.planning.priority != null ? `приоритет ${row.planning.priority}` : null,
                      row.planning.signal_type,
                    ].filter(Boolean).join(' · ')}
                  >
                    DBR{row.planning.priority != null ? ` ${row.planning.priority}` : ''}
                  </span>
                )}
              </span>
            </td>
            <td className="numCell">
              <strong>{qty(row.remaining_qty)}</strong>
              <span>/ {qty(row.quantity)} {row.unit || ''}</span>
            </td>
            <td className="dateCell">
              <span>
                {row.planning?.contour === 'dbr_feeder' ? 'Нужно' : 'С'}: {dateRu(row.planning?.required_date || row.planned_start_date) || '—'}
              </span>
              <span>По: {dateRu(row.planned_finish_date) || '—'}</span>
              <ForecastShift row={row} />
            </td>
            <td>
              <strong>{row.workshop_name || 'Не назначен'}</strong>
              <span className="muted">{row.stage_name || ''}</span>
            </td>
            <td>
              <select aria-label={`Статус заказа ${orderMainLine(row)}`} value={productionStatusSelectValue(row.status)} onChange={(e) => onChangeStatus(row, e.target.value)} onClick={(e) => e.stopPropagation()}>
                {manualProductionStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </td>
            <td>
              <span className={`pill ${row.coverage_status || row.status || 'unknown'}`}>
                {row.coverage_label || coverageLabels[String(row.coverage_status || row.status || '')] || row.coverage_status || row.status || '—'}
              </span>
              {!!row.issue_count && <span className="muted issueCount">док. {row.issue_count}</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
