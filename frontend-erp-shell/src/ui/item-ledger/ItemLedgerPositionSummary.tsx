import { StatusBadge } from '../kit'
import { qty } from '../../lib/format'
import type { ItemLedgerPosition } from '../../domain/itemLedger'

type Props = {
  position: ItemLedgerPosition
}

const flagToneMap: Record<string, string> = {
  on_hand_negative: 'shortage',
  has_uncovered: 'warning',
  reconcile_pending: 'to_move',
}

const flagLabelMap: Record<string, string> = {
  on_hand_negative: 'Остаток отрицательный',
  has_uncovered: 'Есть дефицит',
  reconcile_pending: 'Есть расхождения',
}

function numberCell(value: number) {
  return <strong>{qty(value)}</strong>
}

function renderFlag(flag: string, active: boolean) {
  if (!active) return null
  return <StatusBadge tone={flagToneMap[flag] || 'success'}>{flagLabelMap[flag] || flag}</StatusBadge>
}

export function ItemLedgerPositionSummary({ position }: Props) {
  const poolLabel = position.pool_key.endsWith('::default') ? 'Основной' : 'Специальный'
  return (
    <section aria-label={`Сводка позиции номенклатуры ${position.item_code}`}>
      <h3>Сводка позиции</h3>
      <p>
        <strong>{position.item_code}</strong>{' '}
        <span>{position.item_name}</span>
      </p>
      <p>
        <strong>Контур:</strong> <span title={position.pool_key}>{poolLabel}</span>
      </p>
      <div className="muted" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {renderFlag('on_hand_negative', position.flags.on_hand_negative)}
        {renderFlag('has_uncovered', position.flags.has_uncovered)}
        {renderFlag('reconcile_pending', position.flags.reconcile_pending)}
      </div>
      <table className="journalTable">
        <tbody>
          <tr>
            <th>На складе</th>
            <td className="numCell">{numberCell(position.on_hand)}</td>
          </tr>
          <tr>
            <th>Заказы поставщика</th>
            <td className="numCell">{numberCell(position.incoming_supplier)}</td>
          </tr>
          <tr>
            <th>Заказы в производство</th>
            <td className="numCell">{numberCell(position.incoming_wip)}</td>
          </tr>
          <tr>
            <th>Всего в заказах</th>
            <td className="numCell">{numberCell(position.incoming)}</td>
          </tr>
          <tr>
            <th>Активные резервы</th>
            <td className="numCell">{numberCell(position.reserved_soft)}</td>
          </tr>
          <tr>
            <th>Доступно</th>
            <td className="numCell">{numberCell(position.available)}</td>
          </tr>
          <tr>
            <th>Проекция</th>
            <td className="numCell">{numberCell(position.projected)}</td>
          </tr>
          <tr>
            <th>Не покрыто</th>
            <td className="numCell">{numberCell(position.uncovered)}</td>
          </tr>
        </tbody>
      </table>
      <h4>Остаток по складам</h4>
      <table className="journalTable" aria-label="Остаток по складам">
        <thead>
          <tr>
            <th>Склад</th>
            <th>Остаток</th>
          </tr>
        </thead>
        <tbody>
          {position.on_hand_by_warehouse.map((warehouse) => (
            <tr key={warehouse.warehouse_ref1c}>
              <td title={warehouse.warehouse_ref1c}>{warehouse.warehouse_name || 'Склад 1С'}</td>
              <td className="numCell">{qty(warehouse.qty)}</td>
            </tr>
          ))}
          {!position.on_hand_by_warehouse.length && (
            <tr>
              <td colSpan={2} className="emptyDetail">Склады не заданы</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  )
}
