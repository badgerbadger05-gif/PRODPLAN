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
            <span>Остаток</span><strong>{qty(position.on_hand)} {unit}</strong>
            <span>Резервы</span><strong>{qty(position.reserved_soft)} {unit}</strong>
            <span>Заказы поставщика</span><strong>{qty(position.incoming_supplier)} {unit}</strong>
            <span>Заказы в производство</span><strong>{qty(position.incoming_wip)} {unit}</strong>
            <span>Доступно</span><strong>{qty(position.available)} {unit}</strong>
            <span>Проекция</span><strong>{qty(position.projected)} {unit}</strong>
            <span>Не покрыто</span>
            <strong className={position.uncovered > 0 ? 'mrpRemainingWarn' : 'mrpRemainingOk'}>
              {qty(position.uncovered)} {unit}
            </strong>
          </div>
          {!!position.on_hand_by_warehouse.length && (
            <div className="itemLedgerRecords">
              <strong>Остатки по складам</strong>
              {position.on_hand_by_warehouse.map((row) => (
                <div className="itemLedgerRecord" key={row.warehouse_ref1c}>
                  <span>{row.warehouse_name || row.warehouse_ref1c}</span>
                  <b>{qty(row.qty)} {unit}</b>
                </div>
              ))}
            </div>
          )}
          <div className="itemLedgerRecords">
            <strong>Активные резервы</strong>
            {(reservations?.rows ?? []).slice(0, 8).map((row) => (
              <div className="itemLedgerRecord" key={row.reservation_id}>
                <span>{row.plan_name || `MRP #${row.run_id ?? '—'}`} · треб. #{row.requirement_id}</span>
                <b>{qty(row.replenishment_remaining_qty)} / {qty(row.reserved_qty)} {unit}</b>
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
                <b>{qty(row.open_qty)} / {qty(row.ordered_qty)} {unit}</b>
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
