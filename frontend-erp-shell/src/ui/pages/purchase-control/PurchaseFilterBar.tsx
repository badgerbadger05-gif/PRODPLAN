import {
  purchaseLineStatusOptions,
  supplyPhaseOptions,
  type PurchaseFilters,
  type PurchaseSupplierOption,
} from '../../../domain/purchaseControl'
import { tableColumnStyle, tableMinWidth } from '../../tableDoctype'
import { purchaseOrderColumns } from './purchaseOrdersDoctype'

type Props = {
  filters: PurchaseFilters
  suppliers: PurchaseSupplierOption[]
  states: string[]
  onChange: (filters: PurchaseFilters, submit?: boolean) => void
  onSubmit: () => void
}

export function PurchaseFilterBar({ filters, suppliers, states, onChange, onSubmit }: Props) {
  return (
    <table className="journalTable columnFilterTable productionOrdersTable" style={{ minWidth: tableMinWidth(purchaseOrderColumns) }}>
      <colgroup>
        {purchaseOrderColumns.map((column) => (
          <col key={column.key} style={tableColumnStyle(column)} />
        ))}
      </colgroup>
      <tbody>
        <tr>
          <td className="checkCol"></td>
          <td colSpan={2}>
            <div className="columnFilterSearch">
              <label className="columnFilterControl">
                <span>Поиск</span>
                <input value={filters.search} onChange={(e) => onChange({ ...filters, search: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && onSubmit()} />
              </label>
              <button className="filterBtn" onClick={onSubmit}>Сформировать</button>
            </div>
          </td>
          <td>
            <label className="columnFilterControl">
              <span>Поставщик</span>
              <select value={filters.supplier_id} onChange={(e) => onChange({ ...filters, supplier_id: e.target.value }, true)}>
                <option value="">Все</option>
                {suppliers.map((row) => <option key={row.supplier_id} value={row.supplier_id}>{row.supplier_name}</option>)}
              </select>
            </label>
          </td>
          <td>
            <label className="columnFilterControl" title="Фаза движения товара: Нет товара / Товар в пути / На складе">
              <span>Фаза</span>
              <select value={filters.phase} onChange={(e) => onChange({ ...filters, phase: e.target.value }, true)}>
                <option value="">Все</option>
                {supplyPhaseOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </td>
          <td></td>
          <td>
            <label className="columnFilterControl" title="Показывать только незавершённые заказы (закрыты лишь терминальные статусы 1С: «Отменен», «Завершен»)">
              <span>Активные</span>
              <select value={filters.active_only ? '1' : ''} onChange={(e) => onChange({ ...filters, active_only: e.target.value === '1' }, true)}>
                <option value="1">Только активные</option>
                <option value="">Все</option>
              </select>
            </label>
          </td>
          <td>
            <label className="columnFilterControl">
              <span>Статус 1С</span>
              <select value={filters.state} onChange={(e) => onChange({ ...filters, state: e.target.value }, true)}>
                <option value="">Все</option>
                {states.map((state) => <option key={state} value={state}>{state}</option>)}
              </select>
            </label>
          </td>
          <td>
            <label className="columnFilterControl">
              <span>Статус</span>
              <select value={filters.line_status} onChange={(e) => onChange({ ...filters, line_status: e.target.value }, true)}>
                <option value="">Все</option>
                {purchaseLineStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </td>
          <td></td>
        </tr>
      </tbody>
    </table>
  )
}
