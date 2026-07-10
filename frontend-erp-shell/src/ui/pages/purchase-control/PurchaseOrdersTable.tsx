import {
  purchaseIdsForRow,
  purchaseLineStatusLabel,
  purchaseLineStatusPillClass,
  supplyPhaseLabel,
  supplyPhasePillClass,
  type PurchaseRow,
} from '../../../domain/purchaseControl'
import { dateRu, qty } from '../../../lib/format'
import { sortGlyph, tableColumnStyle, tableMinWidth, type TableSortState } from '../../tableDoctype'
import { purchaseOrderColumns, type PurchaseOrderSortKey } from './purchaseOrdersDoctype'

type Props = {
  rows: PurchaseRow[]
  activeRow: PurchaseRow | null
  selectedPurchaseRowKeys: Set<string>
  sort: TableSortState<PurchaseOrderSortKey>
  onSelectPurchaseRowKeys: (rowKeys: Set<string>) => void
  onActivate: (rowKey: string) => void
  onToggleSort: (key: PurchaseOrderSortKey) => void
}

export function PurchaseOrdersTable({ rows, activeRow, selectedPurchaseRowKeys, sort, onSelectPurchaseRowKeys, onActivate, onToggleSort }: Props) {
  return (
    <table className="journalTable productionOrdersTable" style={{ minWidth: tableMinWidth(purchaseOrderColumns) }}>
      <colgroup>
        {purchaseOrderColumns.map((column) => (
          <col key={column.key} className={column.grow ? 'growCol' : undefined} style={tableColumnStyle(column)} />
        ))}
      </colgroup>
      <thead>
        <tr>
          {purchaseOrderColumns.map((column) => (
            <th key={column.key} className={column.className} style={tableColumnStyle(column)}>
              {column.sortable ? (
                <button type="button" className="tableSortButton" onClick={() => onToggleSort(column.key as PurchaseOrderSortKey)}>
                  {column.title}{sortGlyph(sort, column.key as PurchaseOrderSortKey)}
                </button>
              ) : column.title}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const purchaseIds = purchaseIdsForRow(row)
          const purchaseTitle = purchaseIds.map((id) => `MRP #${id}`).join(', ')
          return (
          <tr key={row.row_key} className={row.row_key === activeRow?.row_key ? 'activeRow' : ''} onClick={() => onActivate(row.row_key)}>
            <td className="checkCol">
              {row.line_status === 'to_order' && purchaseIds.length > 0 && (
                <input
                  type="checkbox"
                  checked={selectedPurchaseRowKeys.has(row.row_key)}
                  onChange={(e) => {
                    const next = new Set(selectedPurchaseRowKeys)
                    if (e.target.checked) next.add(row.row_key)
                    else next.delete(row.row_key)
                    onSelectPurchaseRowKeys(next)
                  }}
                  onClick={(e) => e.stopPropagation()}
                />
              )}
            </td>
            <td className={`orderCell ${row.order_ref1c ? 'oneCOrderCell' : ''}`}>
              {row.line_status === 'to_order' ? (
                <>
                  <strong title={purchaseTitle}>
                    MRP #{row.purchase_id ?? purchaseIds[0]}{purchaseIds.length > 1 ? ` +${purchaseIds.length - 1}` : ''}
                  </strong>
                  <span title={`заказ ${dateRu(row.order_date) || '—'}`}>заказ {dateRu(row.order_date) || '—'}</span>
                </>
              ) : (
                <>
                  <strong title={row.order_number}>{row.order_number}</strong>
                  <span title={`${dateRu(row.order_date) || ''}${row.source === 'mrp' ? ' · из MRP' : ''}`}>{dateRu(row.order_date) || ''}{row.source === 'mrp' ? ' · из MRP' : ''}</span>
                </>
              )}
            </td>
            <td className="itemCell">
              <strong title={row.item_name}>{row.item_name}</strong>
              <span title={row.item_article || row.item_code || ''}>{row.item_article || row.item_code || ''}</span>
            </td>
            <td>
              <strong title={row.supplier_name || 'Не указан'}>{row.supplier_name || 'Не указан'}</strong>
            </td>
            <td className="numCell">
              <strong>{qty(row.remaining_qty)}</strong>
              <span>/ {qty(row.quantity)} {row.unit || ''}</span>
            </td>
            <td className="numCell">
              {row.received_qty > 0 ? qty(row.received_qty) : <span className="muted">—</span>}
            </td>
            <td className="dateCell">
              {row.line_status === 'to_order' ? (
                <span title="Дата потребности по MRP">потр. {dateRu(row.need_date) || '—'}</span>
              ) : (
                <span>{dateRu(row.delivery_date) || '—'}</span>
              )}
              {row.overdue_days > 0 && (
                <span className="forecastShift late" title="Дней просрочки от плановой даты">+{row.overdue_days} дн</span>
              )}
            </td>
            <td>
              {row.order_state_name ? (
                <span
                  className={`pill ${supplyPhasePillClass(row.supply_phase)}`}
                  title={`${row.order_state_name} · фаза «${supplyPhaseLabel(row.supply_phase)}»${row.counts_in_mrp ? ' · учитывается в MRP' : ' · не учитывается в MRP'}`}
                >
                  {row.order_state_name}
                </span>
              ) : (
                <span className="muted">{row.line_status === 'to_order' ? 'Не заказан' : '—'}</span>
              )}
            </td>
            <td>
              <span className={`pill ${purchaseLineStatusPillClass(row.line_status)}`}>
                {purchaseLineStatusLabel(row.line_status)}
              </span>
            </td>
            <td className="numCell">
              {row.amount > 0 ? qty(row.amount) : <span className="muted">—</span>}
            </td>
          </tr>
          )
        })}
      </tbody>
    </table>
  )
}
