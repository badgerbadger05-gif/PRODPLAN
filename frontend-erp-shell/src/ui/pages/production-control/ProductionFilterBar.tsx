import { coverageLabels, productionStatusOptions, type ProductionFilters } from '../../../domain/productionControl'
import type { ProductionResource } from '../../../domain/resources'
import { tableColumnStyle, tableMinWidth } from '../../tableDoctype'
import { productionOrderColumns, type ProductionOrderSortKey } from './productionOrdersDoctype'

type Props = {
  filters: ProductionFilters
  resources: ProductionResource[]
  onChange: (filters: ProductionFilters, submit?: boolean) => void
  onSubmit: () => void
  onToggleSort: (key: ProductionOrderSortKey) => void
}

const coverageOptions = ['shortage', 'partial', 'ready', 'to_move', 'assembled'] as const

export function ProductionFilterBar({ filters, resources, onChange, onSubmit, onToggleSort }: Props) {
  return (
    <table className="journalTable columnFilterTable productionOrdersTable" style={{ minWidth: tableMinWidth(productionOrderColumns) }}>
      <colgroup>
        {productionOrderColumns.map((column) => (
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
          <td></td>
          <td>
            <button className="filterBtn columnFilterButton" onClick={() => onToggleSort('planned_start_date')}>
              План {filters.sort_by === 'planned_start_date' ? (filters.sort_dir === 'asc' ? '▲' : '▼') : ''}
            </button>
          </td>
          <td>
            <label className="columnFilterControl">
              <span>Участок</span>
              <select value={filters.workshop_id} onChange={(e) => onChange({ ...filters, workshop_id: e.target.value }, true)}>
                <option value="">Все</option>
                {resources.map((row) => <option key={row.resource_id} value={row.resource_id}>{row.resource_name}</option>)}
              </select>
            </label>
          </td>
          <td>
            <label className="columnFilterControl">
              <span>Статус</span>
              <select value={filters.status} onChange={(e) => onChange({ ...filters, status: e.target.value }, true)}>
                <option value="">Все</option>
                {productionStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </td>
          <td>
            <label className="columnFilterControl">
              <span>Обеспечение</span>
              <select value={filters.coverage_status} onChange={(e) => onChange({ ...filters, coverage_status: e.target.value }, true)}>
                <option value="">Все</option>
                {coverageOptions.map((value) => <option key={value} value={value}>{coverageLabels[value]}</option>)}
              </select>
            </label>
          </td>
        </tr>
      </tbody>
    </table>
  )
}
