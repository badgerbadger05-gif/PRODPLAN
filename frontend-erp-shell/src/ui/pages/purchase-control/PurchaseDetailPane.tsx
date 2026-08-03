import { useEffect, useState } from 'react'
import {
  purchaseLineStatusLabel,
  purchaseLineStatusPillClass,
  supplyPhaseLabel,
  supplyPhasePillClass,
  type PurchaseOrderCard,
  type PurchaseRow,
} from '../../../domain/purchaseControl'
import { dateRu, qty } from '../../../lib/format'
import { getPurchaseOrderCard } from '../../../services/purchaseControl'

type Props = {
  activeRow: PurchaseRow | null
  embedded?: boolean
}

export function PurchaseDetailPane({ activeRow, embedded = false }: Props) {
  const formatCoveragePercent = (value: number | null | undefined) => (value == null ? 'н/д' : `${qty(value)}%`)

  const [card, setCard] = useState<PurchaseOrderCard | null>(null)
  const [cardError, setCardError] = useState('')
  const [cardLoading, setCardLoading] = useState(false)

  const orderId = activeRow?.order_id ?? null

  useEffect(() => {
    let cancelled = false
    setCard(null)
    setCardError('')
    if (!orderId) return
    setCardLoading(true)
    getPurchaseOrderCard(orderId)
      .then((data) => { if (!cancelled) setCard(data) })
      .catch((e) => { if (!cancelled) setCardError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setCardLoading(false) })
    return () => { cancelled = true }
  }, [orderId])

  if (!activeRow) {
    const empty = (
      <>
        <h2>Карточка строки</h2>
        <div className="emptyDetail">Выберите строку журнала</div>
      </>
    )
    return embedded ? empty : <aside className="detailPane">{empty}</aside>
  }

  const content = (
    <>
      <h2>{activeRow.row_generator === 'mrp_reservation' ? 'MRP-потребность' : 'Карточка строки'}</h2>
      <div className="detailTitle">{activeRow.item_name}</div>
      <div className="detailMeta">{activeRow.item_article || activeRow.item_code}</div>
      <div className="detailGrid">
        {activeRow.row_generator === 'mrp_reservation' ? (
          <>
            <span>Генератор</span><strong>{activeRow.row_generator === 'mrp_reservation' ? 'Под заказ (MRP)' : activeRow.row_generator || '—'}</strong>
            <span>Планы</span><strong>{activeRow.run_ids?.length ?? (activeRow.run_id ? 1 : 0)}</strong>
            <span>Дата заказа</span><strong>{dateRu(activeRow.order_date) || '—'}</strong>
            <span>Дата потребности</span><strong>{dateRu(activeRow.need_date) || '—'}</strong>
            <span>Общая потребность / к заказу</span>
            <strong>
              {activeRow.required_qty == null ? '—' : qty(activeRow.required_qty)} / {activeRow.to_order_qty == null ? '—' : qty(activeRow.to_order_qty)}
            </strong>
            <span>Поступило сейчас</span>
            <strong>{activeRow.realized_qty == null ? '—' : qty(activeRow.realized_qty)}</strong>
            <span>Покрыто заказами</span>
            <strong>{activeRow.open_order_covered_qty == null ? '—' : `${qty(activeRow.open_order_covered_qty)} (${formatCoveragePercent(activeRow.open_order_covered_pct)})`}</strong>
            <span>К заказу</span>
            <strong>{activeRow.to_order_qty == null ? '—' : `${qty(activeRow.to_order_qty)} (${formatCoveragePercent(activeRow.to_order_pct)})`}</strong>
          </>
        ) : (
          <>
            <span>Заказ</span><strong>{activeRow.order_number}</strong>
            <span>Дата заказа</span><strong>{dateRu(activeRow.order_date) || '—'}</strong>
            <span>Дата поставки</span><strong>{dateRu(activeRow.delivery_date) || '—'}</strong>
            <span>Источник</span><strong>{activeRow.row_generator || '—'}</strong>
            <span>Статус 1С</span>
            <strong>
              {activeRow.order_state_name ? (
                <span
                  className={`pill ${supplyPhasePillClass(activeRow.supply_phase)}`}
                  title={activeRow.counts_in_mrp ? 'Учитывается в расчёте MRP' : 'Не учитывается в расчёте MRP'}
                >
                  {activeRow.order_state_name}
                </span>
              ) : '—'}
            </strong>
            <span>Фаза</span><strong>{supplyPhaseLabel(activeRow.supply_phase)}</strong>
          </>
        )}
        <span>Поставщик</span><strong>{activeRow.supplier_name || 'Не указан'}</strong>
        {activeRow.row_generator !== 'mrp_reservation' && (
          <>
            <span>Заказано</span><strong>{qty(activeRow.quantity)} {activeRow.unit || ''}</strong>
            <span>Поступило</span>
            <strong>
              {activeRow.received_qty === null
                ? <span className="muted" title="Факт поступления отсутствует в снимке">н/д</span>
                : `${qty(activeRow.received_qty)} ${activeRow.unit || ''}`}
            </strong>
            <span>Осталось</span><strong>{qty(activeRow.remaining_qty)} {activeRow.unit || ''}</strong>
          </>
        )}
        {activeRow.price !== null && activeRow.price > 0 && (<><span>Цена</span><strong>{qty(activeRow.price)}</strong></>)}
        {activeRow.amount !== null && activeRow.amount > 0 && (<><span>Сумма</span><strong>{qty(activeRow.amount)}</strong></>)}
        <span>Статус</span>
        <strong>
          {activeRow.line_status === 'unavailable'
            ? <span className="muted">н/д — факт поступления недоступен</span>
            : (
              <span className={`pill ${purchaseLineStatusPillClass(activeRow.line_status)}`}>
                {purchaseLineStatusLabel(activeRow.line_status)}
              </span>
            )}
        </strong>
      </div>

      {orderId && (
        <>
          <h2 style={{ marginTop: 16 }}>Заказ целиком</h2>
          {cardLoading && <div className="hintLine">Загрузка…</div>}
          {cardError && <div className="errorLine">{cardError}</div>}
          {card && (
            <>
              <div className="detailGrid">
                <span>Поставщик</span><strong>{card.order.supplier_name || 'Не указан'}</strong>
                <span>Сумма документа</span><strong>{qty(card.order.document_amount)}</strong>
                <span>Источник</span><strong>{card.order.source === 'mrp' ? 'Создан из MRP' : 'Создан в 1С'}</strong>
              </div>
              <div className="detailList">
                {card.lines.map((line) => (
                  <div key={line.row_key} className="detailListRow" title={`${line.item_article || line.item_code || ''}`}>
                    <span className="detailListName">{line.item_name}</span>
                    <span>{line.received_qty === null ? 'н/д' : qty(line.received_qty)} / {qty(line.quantity)}</span>
                    {line.line_status === 'unavailable'
                      ? <span className="muted">н/д</span>
                      : (
                        <span className={`miniPill ${purchaseLineStatusPillClass(line.line_status)}`}>
                          {purchaseLineStatusLabel(line.line_status)}
                        </span>
                      )}
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </>
  )
  return embedded ? content : <aside className="detailPane">{content}</aside>
}
