import {
  coverageLabels,
  type MaterialIssueDetail,
  type TransferIssueRow,
} from '../../domain/productionControl'
import { qty } from '../../lib/format'
import { DoctypePage, useDoctypeList } from '../doctype'
import type { AccessSubject } from '../doctype/permissions'
import type { DoctypeListState } from '../doctype/useDoctypeList'
import { tableColumnStyle, tableMinWidth } from '../tableDoctype'
import {
  orderMainLine,
  transferRequestsDoctype,
  transferStatusLabels,
  warehouseLabel,
  type TransferRequestFilters,
} from './transferRequestsDoctype'

type TransferState = DoctypeListState<TransferIssueRow, TransferRequestFilters, MaterialIssueDetail>

const access: AccessSubject = {
  roles: ['shopfloor'],
  permissions: ['material_issue.assemble_post_1c', 'production.propose'],
}

function TransferFilters({ state }: { state: TransferState }) {
  const warehouses = (state.listMeta.source_warehouses ?? []) as Array<{
    warehouse_ref1c: string
    warehouse_name?: string | null
  }>

  return (
    <table
      className="journalTable columnFilterTable transferTable"
      style={{ minWidth: tableMinWidth(transferRequestsDoctype.columns) }}
    >
      <colgroup>
        {transferRequestsDoctype.columns.map((column) => (
          <col key={column.key} style={tableColumnStyle(column)} />
        ))}
      </colgroup>
      <tbody>
        <tr>
          <td className="checkCol"></td>
          <td colSpan={3}>
            <div className="columnFilterSearch">
              <label className="columnFilterControl">
                <span>Поиск</span>
                <input
                  value={state.filters.search}
                  onChange={(event) => state.setFilter('search', event.target.value)}
                  onKeyDown={(event) => { if (event.key === 'Enter') state.applyFilters() }}
                />
              </label>
              <button onClick={state.applyFilters} disabled={state.loading}>Найти</button>
            </div>
          </td>
          <td></td>
          <td>
            <label className="columnFilterControl">
              <span>Склад</span>
              <select
                value={state.filters.sourceWarehouseRef}
                onChange={(event) => state.setFilter('sourceWarehouseRef', event.target.value)}
              >
                <option value="">Все</option>
                {warehouses.map((warehouse) => (
                  <option key={warehouse.warehouse_ref1c} value={warehouse.warehouse_ref1c}>
                    {warehouseLabel(warehouse.warehouse_name, warehouse.warehouse_ref1c)}
                  </option>
                ))}
              </select>
            </label>
          </td>
          <td></td>
          <td>
            <label className="columnFilterControl">
              <span>Статус</span>
              <select value={state.filters.status} onChange={(event) => state.setFilter('status', event.target.value)}>
                <option value="">Все</option>
                <option value="draft">Черновик</option>
                <option value="requested">Заявка</option>
                <option value="exported">В 1С</option>
                <option value="posted">Собрано</option>
                <option value="error">Ошибка</option>
              </select>
            </label>
          </td>
        </tr>
      </tbody>
    </table>
  )
}

function TransferDetail({ detail }: { detail: MaterialIssueDetail }) {
  return (
    <>
      <h2>Детали к сборке</h2>
      <div className="detailTitle">{detail.item_name}</div>
      <div className="detailMeta">{detail.one_c_number || detail.document_number} · {orderMainLine(detail)}</div>
      <div className="detailGrid">
        <span>Статус</span><strong>{transferStatusLabels[detail.status] || detail.status}</strong>
        <span>Заказ 1С</span><strong>{detail.order_ref1c ? (detail.order_one_c_number || detail.order_number) : 'не выгружен'}</strong>
        <span>Обеспечение</span><strong>{coverageLabels[String(detail.line_status || '')] || detail.line_status || '—'}</strong>
        <span>Отправитель</span><strong>{warehouseLabel(detail.source_warehouse_name, detail.source_warehouse_ref1c)}</strong>
        <span>Получатель</span><strong>{warehouseLabel(detail.destination_warehouse_name, detail.warehouse_ref1c)}</strong>
        <span>Номер 1С</span><strong>{detail.one_c_number || '—'}</strong>
        <span>Ref 1С</span><strong>{detail.exported_ref1c || '—'}</strong>
        {detail.export_error && <span>Ошибка 1С</span>}
        {detail.export_error && <strong>{detail.export_error}</strong>}
      </div>
      <h3>Комплектующие</h3>
      <div className="materialsList">
        {detail.lines.map((line) => (
          <div className="materialRow" key={line.line_id}>
            <div>
              <strong>{line.item_name}</strong>
              <span>{line.item_article || line.item_code}</span>
            </div>
            <div className="matNums">
              <span>нужно {qty(line.required_qty)}</span>
              <span>выдано {qty(line.issued_qty)}</span>
            </div>
            <span className={`miniPill ${line.line_status === 'issued' ? 'assembled' : 'to_move'}`}>{line.line_status || 'planned'}</span>
          </div>
        ))}
        {!detail.lines.length && <div className="emptyDetail">Комплектующие не загружены</div>}
      </div>
    </>
  )
}

export function TransferRequestsPage() {
  const state = useDoctypeList(transferRequestsDoctype, { access })

  return (
    <DoctypePage
      doctype={transferRequestsDoctype}
      state={state}
      access={access}
      breadcrumbs="Производство / Заявки на перемещение"
      renderFilters={(current) => <TransferFilters state={current} />}
      renderDetail={(value) => <TransferDetail detail={value as MaterialIssueDetail} />}
    />
  )
}
