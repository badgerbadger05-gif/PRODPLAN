import { coverageLabels, productionStatusOptions, productionStatusSelectValue, type OrderRow } from '../../../domain/productionControl'
import { dateRu, qty } from '../../../lib/format'
import { sortGlyph, tableColumnStyle, tableMinWidth, type TableSortState } from '../../tableDoctype'
import { productionOrderColumns, type ProductionOrderSortKey } from './productionOrdersDoctype'

type Props = {
  rows: OrderRow[]
  activeRow: OrderRow | null
  selectedIds: Set<number>
  sort: TableSortState<ProductionOrderSortKey>
  onSelectIds: (ids: Set<number>) => void
  onActivate: (id: number) => void
  onOpenMaterials: (row: OrderRow) => void
  onChangeStatus: (row: OrderRow, status: string) => void
  onToggleSort: (key: ProductionOrderSortKey) => void
}

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

export function ProductionOrdersTable({ rows, activeRow, selectedIds, sort, onSelectIds, onActivate, onOpenMaterials, onChangeStatus, onToggleSort }: Props) {
  return (
    <table className="journalTable productionOrdersTable" style={{ minWidth: tableMinWidth(productionOrderColumns) }}>
      <colgroup>
        {productionOrderColumns.map((column) => (
          <col key={column.key} className={column.grow ? 'growCol' : undefined} style={tableColumnStyle(column)} />
        ))}
      </colgroup>
      <thead>
        <tr>
          {productionOrderColumns.map((column) => (
            <th key={column.key} className={column.className} style={tableColumnStyle(column)}>
              {column.sortable ? (
                <button type="button" className="tableSortButton" onClick={() => onToggleSort(column.key as ProductionOrderSortKey)}>
                  {column.title}{sortGlyph(sort, column.key as ProductionOrderSortKey)}
                </button>
              ) : column.title}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.product_id} className={row.product_id === activeRow?.product_id ? 'activeRow' : ''} onClick={() => onActivate(row.product_id)} onDoubleClick={() => onOpenMaterials(row)}>
            <td className="checkCol">
              <input
                type="checkbox"
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
              <strong>{row.order_number}</strong>
              <span>{orderSubline(row)}</span>
            </td>
            <td className="itemCell">
              <strong>{row.item_name}</strong>
              <span>{row.item_article || row.item_code || ''}</span>
            </td>
            <td className="numCell">
              <strong>{qty(row.remaining_qty)}</strong>
              <span>/ {qty(row.quantity)} {row.unit || ''}</span>
            </td>
            <td className="dateCell">
              <span>С: {dateRu(row.planned_start_date) || '—'}</span>
              <span>По: {dateRu(row.planned_finish_date) || '—'}</span>
              <ForecastShift row={row} />
            </td>
            <td>
              <strong>{row.workshop_name || 'Не назначен'}</strong>
              <span className="muted">{row.stage_name || ''}</span>
            </td>
            <td>
              <select value={productionStatusSelectValue(row.status)} onChange={(e) => onChangeStatus(row, e.target.value)} onClick={(e) => e.stopPropagation()}>
                {productionStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
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
