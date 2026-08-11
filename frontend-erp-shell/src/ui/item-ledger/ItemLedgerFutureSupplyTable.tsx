import { dateRu, qty } from '../../lib/format'
import type { ItemLedgerFutureSupplyResponse } from '../../domain/itemLedger'

type Props = {
  rows: ItemLedgerFutureSupplyResponse['rows']
}

const supplyKindLabel: Record<string, string> = {
  supplier_order: 'Заказ поставщику',
  wip_order: 'Заказ в производство',
}

export function ItemLedgerFutureSupplyTable({ rows }: Props) {
  return (
    <table className="journalTable" aria-label="Живые заказы по номенклатуре">
      <thead>
        <tr>
          <th>Тип</th>
          <th>Заказ</th>
          <th className="numCell">Заказано</th>
          <th className="numCell">Получено</th>
          <th className="numCell">Осталось</th>
          <th>Дата поставки</th>
          <th>Склад назначения</th>
          <th>Статус</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <td>{supplyKindLabel[row.supply_kind] || row.supply_kind}</td>
            <td title={row.source_ref}>{row.source_number || 'Документ 1С'}</td>
            <td className="numCell">{qty(row.ordered_qty)}</td>
            <td className="numCell">{qty(row.received_qty)}</td>
            <td className="numCell"><strong>{qty(row.open_qty)}</strong></td>
            <td>{dateRu(row.eta_date) || '—'}</td>
            <td title={row.destination_warehouse_ref1c}>{row.destination_warehouse_name || 'Склад 1С'}</td>
            <td title={row.source_state_key}>{row.source_state_name || 'Открыт'}</td>
          </tr>
        ))}
        {!rows.length && (
          <tr>
            <td colSpan={8}><div className="emptyDetail">Открытых заказов нет</div></td>
          </tr>
        )}
      </tbody>
    </table>
  )
}
