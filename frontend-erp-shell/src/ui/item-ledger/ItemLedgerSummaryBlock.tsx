import { useEffect, useState } from 'react'
import type {
  ItemLedgerFutureSupplyResponse,
  ItemLedgerPosition,
  ItemLedgerReservationsResponse,
} from '../../domain/itemLedger'
import { dateRu, qty } from '../../lib/format'
import {
  getItemLedgerFutureSupply,
  getItemLedgerPosition,
  getItemLedgerReservations,
} from '../../services/itemLedger'

type Props = {
  itemId: number
  unit?: string | null
}

function supplyLabel(source: string) {
  return source === 'supplier_order' ? 'Заказ поставщику' : 'Заказ на производство'
}

export function ItemLedgerSummaryBlock({ itemId, unit }: Props) {
  const displayUnit = unit && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(unit)
    ? unit
    : ''
  const amount = (value: number) => `${qty(value)} ${displayUnit}`.trim()
  const [position, setPosition] = useState<ItemLedgerPosition | null>(null)
  const [reservations, setReservations] = useState<ItemLedgerReservationsResponse | null>(null)
  const [futureSupply, setFutureSupply] = useState<ItemLedgerFutureSupplyResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setPosition(null)
    setReservations(null)
    setFutureSupply(null)
    Promise.all([
      getItemLedgerPosition(itemId, controller.signal),
      getItemLedgerReservations(itemId, { status: 'active' }, controller.signal),
      getItemLedgerFutureSupply(itemId, controller.signal),
    ])
      .then(([nextPosition, nextReservations, nextFutureSupply]) => {
        setPosition(nextPosition)
        setReservations(nextReservations)
        setFutureSupply(nextFutureSupply)
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [itemId])

  return (
    <div className="itemLedgerBlock">
      <div className="itemLedgerTitle">
        <span>Ledger по номенклатуре</span>
        {position && <span>поколение #{position.truth_meta.ledger_generation}</span>}
      </div>
      {loading && <div className="emptyDetail">Загрузка Ledger…</div>}
      {error && <div className="errorLine" role="alert">{error}</div>}
      {!loading && !error && position && (
        <>
          <div className="mrpCoverageGrid itemLedgerGrid">
            <span>Остаток</span><strong>{amount(position.on_hand)}</strong>
            <span>Резервы</span><strong>{amount(position.reserved_soft)}</strong>
            <span>Заказы поставщика</span><strong>{amount(position.incoming_supplier)}</strong>
            <span>Заказы в производство</span><strong>{amount(position.incoming_wip)}</strong>
            <span>Доступно</span><strong>{amount(position.available)}</strong>
            <span>Проекция</span><strong>{amount(position.projected)}</strong>
            <span>Не покрыто</span>
            <strong className={position.uncovered > 0 ? 'mrpRemainingWarn' : 'mrpRemainingOk'}>
              {amount(position.uncovered)}
            </strong>
          </div>
          {!!position.on_hand_by_warehouse.length && (
            <div className="itemLedgerRecords">
              <strong>Остатки по складам</strong>
              {position.on_hand_by_warehouse.map((row) => (
                <div className="itemLedgerRecord" key={row.warehouse_ref1c}>
                  <span>{row.warehouse_name || row.warehouse_ref1c}</span>
                  <b>{amount(row.qty)}</b>
                </div>
              ))}
            </div>
          )}
          <div className="itemLedgerRecords">
            <strong>Активные резервы</strong>
            {(reservations?.rows ?? []).slice(0, 8).map((row) => (
              <div className="itemLedgerRecord" key={row.reservation_id}>
                <span>{row.plan_name || `MRP #${row.run_id ?? '—'}`} · треб. #{row.requirement_id}</span>
                <b>{amount(row.replenishment_remaining_qty)} / {amount(row.reserved_qty)}</b>
              </div>
            ))}
            {!reservations?.rows.length && <div className="emptyDetail">Живых резервов нет</div>}
            {(reservations?.rows.length ?? 0) > 8 && (
              <div className="itemLedgerMore">Ещё {(reservations?.rows.length ?? 0) - 8}</div>
            )}
          </div>
          <div className="itemLedgerRecords">
            <strong>Живые заказы</strong>
            {(futureSupply?.rows ?? []).slice(0, 8).map((row) => (
              <div className="itemLedgerRecord" key={row.id}>
                <span>
                  {supplyLabel(row.supply_kind)} {row.source_number || row.source_ref || `#${row.id}`} · {dateRu(row.eta_date) || 'без даты'}
                </span>
                <b>{amount(row.open_qty)} / {amount(row.ordered_qty)}</b>
              </div>
            ))}
            {!futureSupply?.rows.length && <div className="emptyDetail">Живых заказов нет</div>}
            {(futureSupply?.rows.length ?? 0) > 8 && (
              <div className="itemLedgerMore">Ещё {(futureSupply?.rows.length ?? 0) - 8}</div>
            )}
          </div>
          <a className="itemLedgerLink" href={`#/ledger/items/${itemId}`}>Открыть полную карточку Ledger</a>
        </>
      )}
    </div>
  )
}
