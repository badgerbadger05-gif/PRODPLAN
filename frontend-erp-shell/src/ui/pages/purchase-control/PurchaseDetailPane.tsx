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
}

export function PurchaseDetailPane({ activeRow }: Props) {
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
    return (
      <aside className="detailPane">
        <h2>Карточка строки</h2>
        <div className="emptyDetail">Выберите строку журнала</div>
      </aside>
    )
  }

  const mrpHref = activeRow.run_id && activeRow.purchase_id
    ? `#/mrp-runs/${activeRow.run_id}?tab=purchases&purchase_id=${activeRow.purchase_id}`
    : null

  return (
    <aside className="detailPane">
      <h2>{activeRow.line_status === 'to_order' ? 'MRP-потребность' : 'Карточка строки'}</h2>
      <div className="detailTitle">{activeRow.item_name}</div>
      <div className="detailMeta">{activeRow.item_article || activeRow.item_code}</div>
      <div className="detailGrid">
        {activeRow.line_status === 'to_order' ? (
          <>
            <span>Закупка MRP</span><strong>#{activeRow.purchase_id}</strong>
            <span>Дата заказа</span><strong>{dateRu(activeRow.order_date) || '—'}</strong>
            <span>Дата потребности</span><strong>{dateRu(activeRow.need_date) || '—'}</strong>
          </>
        ) : (
          <>
            <span>Заказ</span><strong>{activeRow.order_number}</strong>
            <span>Дата заказа</span><strong>{dateRu(activeRow.order_date) || '—'}</strong>
            <span>Дата поставки</span><strong>{dateRu(activeRow.delivery_date) || '—'}</strong>
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
        <span>Заказано</span><strong>{qty(activeRow.quantity)} {activeRow.unit || ''}</strong>
        <span>Поступило</span><strong>{qty(activeRow.received_qty)} {activeRow.unit || ''}</strong>
        <span>Осталось</span><strong>{qty(activeRow.remaining_qty)} {activeRow.unit || ''}</strong>
        {activeRow.price > 0 && (<><span>Цена</span><strong>{qty(activeRow.price)}</strong></>)}
        {activeRow.amount > 0 && (<><span>Сумма</span><strong>{qty(activeRow.amount)}</strong></>)}
        <span>Статус</span>
        <strong>
          <span className={`pill ${purchaseLineStatusPillClass(activeRow.line_status)}`}>
            {purchaseLineStatusLabel(activeRow.line_status)}
          </span>
        </strong>
        {mrpHref && (
          <>
            <span>Источник</span>
            <strong><a href={mrpHref}>MRP прогон #{activeRow.run_id}</a></strong>
          </>
        )}
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
                    <span>{qty(line.received_qty)} / {qty(line.quantity)}</span>
                    <span className={`miniPill ${purchaseLineStatusPillClass(line.line_status)}`}>
                      {purchaseLineStatusLabel(line.line_status)}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </aside>
  )
}
