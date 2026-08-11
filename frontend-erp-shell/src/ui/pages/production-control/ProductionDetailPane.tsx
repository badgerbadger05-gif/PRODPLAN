import { useEffect, useState } from 'react'
import { productionStatusLabel, type MaterialsResponse, type OrderRow } from '../../../domain/productionControl'
import { dateRu, qty } from '../../../lib/format'
import { ItemLedgerSummaryBlock } from '../../item-ledger/ItemLedgerSummaryBlock'

type Props = {
  activeRow: OrderRow | null
  materials: MaterialsResponse | null
  coverageLabels: Record<string, string>
  onLoadMaterials: () => void
  onPrint: () => void
  onProduce: () => void
  onReturnLeftovers: () => void
  onOptimalBatchSave: (itemId: number, value: number | null) => Promise<void>
  launchQuantity: number | null
  onLaunchQuantityChange: (value: number) => void
}

export function ProductionDetailPane({
  activeRow,
  materials,
  coverageLabels,
  onLoadMaterials,
  onPrint,
  onProduce,
  onReturnLeftovers,
  onOptimalBatchSave,
  launchQuantity,
  onLaunchQuantityChange,
}: Props) {
  const [batchValue, setBatchValue] = useState('')
  const [batchSaving, setBatchSaving] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [launchValue, setLaunchValue] = useState('')

  useEffect(() => {
    setBatchValue(activeRow?.optimal_batch != null ? String(activeRow.optimal_batch) : '')
    setBatchError('')
  }, [activeRow?.journal_row_key, activeRow?.optimal_batch])

  useEffect(() => {
    setLaunchValue(launchQuantity == null ? '' : String(launchQuantity))
  }, [activeRow?.journal_row_key, launchQuantity])

  function commitLaunchQuantity() {
    if (activeRow?.work_item_id == null) return
    const value = Number(launchValue)
    const max = activeRow.launchable_qty ?? activeRow.quantity
    if (!Number.isFinite(value) || value <= 0 || value > max) {
      setLaunchValue(String(launchQuantity ?? max))
      return
    }
    onLaunchQuantityChange(value)
  }

  async function handleBatchSave() {
    if (!activeRow?.item_id) return
    const trimmed = batchValue.trim()
    const value = trimmed === '' ? null : Number(trimmed)
    if (value !== null && (Number.isNaN(value) || value < 0)) {
      setBatchError('Некорректное значение')
      return
    }
    setBatchError('')
    setBatchSaving(true)
    try {
      await onOptimalBatchSave(activeRow.item_id, value)
    } catch {
      setBatchError('Ошибка сохранения')
    } finally {
      setBatchSaving(false)
    }
  }

  const rowSource = activeRow?.order_source || activeRow?.source
  const hasMrpCoverage = activeRow?.source_mrp_requirement_id != null && activeRow?.mrp_req_net_qty != null
  const mrpRemaining = activeRow?.mrp_req_remaining_qty
  const activeCoverageStatus = materials?.coverage_status
    || activeRow?.coverage_status
    || activeRow?.status
    || 'unknown'
  const activeCoverageLabel = materials?.coverage_label
    || (activeRow?.coverage_status === 'unknown' ? coverageLabels.unknown : activeRow?.coverage_label)
    || coverageLabels[String(activeCoverageStatus)]
    || activeCoverageStatus
  const activeStatusLabel = activeRow?.product_id == null ? 'Не создан' : productionStatusLabel(activeRow.status)
  const planSourceLabel = activeRow?.source_plan_name
    || (activeRow?.source_plan_id ? `План #${activeRow.source_plan_id}` : '')
  const sourceDisplayLabel = planSourceLabel || activeRow?.launch_source || rowSource || '1C'
  const hasShelfLaunchData = Boolean(
    activeRow?.launch_source
    || activeRow?.shelf_warehouse_ref1c
    || activeRow?.shelf_pull_qty != null
    || activeRow?.shelf_materialized_qty != null
    || activeRow?.shelf_latest_start_date,
  )

  function shelfQty(value?: number | null) {
    return value == null ? '—' : `${qty(value)} ${activeRow?.unit || ''}`.trim()
  }

  function sourceLabel(source?: string | null) {
    if (source === 'supplier_order') return 'Заказ поставщику'
    if (source === 'production_order') return 'Заказ на производство'
    if (source === 'planned_purchase') return 'MRP закупка'
    if (source === 'planned_production') return 'MRP производство'
    return 'Заказ'
  }

  function expectedLine(m: NonNullable<MaterialsResponse['components']>[number]) {
    const reservedOrders = m.reserved_orders ?? []
    if ((m.missing_qty ?? 0) > 0 && reservedOrders.length) {
      return `В резерве: ${reservedOrders.slice(0, 2).map((r) => {
        const ref = r.order_number || `#${r.product_id}`
        return `${ref} — ${qty(r.reserved_qty)} ${m.unit || ''}`
      }).join('; ')}`
    }
    const dates = m.expected_dates?.length ? m.expected_dates : (m.eta_dates ?? []).map((eta) => ({
      source: eta.source,
      order_number: eta.ref,
      date: eta.date,
      qty: eta.qty,
    }))
    if (dates.length) {
      return dates.slice(0, 2).map((eta) => {
        const ref = eta.order_number || ('ref' in eta ? eta.ref : undefined) || 'без номера'
        const when = eta.date ? dateRu(eta.date) : 'дата не указана'
        const amount = eta.qty != null ? `, ${qty(eta.qty)} ${m.unit || ''}` : ''
        return `${sourceLabel(eta.source)} ${ref}: ${when}${amount}`
      }).join('; ')
    }
    const status = String(m.coverage_status || m.availability_status || '')
    return m.coverage_label || coverageLabels[status] || 'Недоступно'
  }

  function activeOrderNumber() {
    return activeRow?.order_prodplan_number || activeRow?.order_number || ''
  }

  function reservationLine(m: NonNullable<MaterialsResponse['components']>[number]) {
    const parts: string[] = []
    const own = m.reserved_for_order_qty ?? 0
    const atWorkshop = m.reserved_at_workshop_qty ?? 0
    const inTransit = m.reserved_in_transit_qty ?? 0
    const other = m.reserved_qty ?? 0
    if (own > 0) {
      const place = atWorkshop > 0 && inTransit > 0
        ? `участок ${qty(atWorkshop)}, в пути ${qty(inTransit)}`
        : atWorkshop > 0
          ? `на участке ${qty(atWorkshop)}`
          : inTransit > 0
            ? `в пути ${qty(inTransit)}`
            : qty(own)
      parts.push(`под эту строку: ${place}`)
    }
    if (other > 0) parts.push(`чужой резерв: ${qty(other)}`)
    return parts.join('; ')
  }

  return (
    <aside className="detailPane">
      <h2>Карточка строки</h2>
      {activeRow ? (
        <>
          <div className="detailTitle">{activeRow.item_name}</div>
          <div className="detailMeta">{activeRow.item_article || activeRow.item_code}</div>
          <div className="detailGrid">
            <span>Заказ</span><strong>{activeOrderNumber()}</strong>
            {activeRow.order_ref1c && (
              <>
                <span>Заказ 1С</span><strong>{activeRow.order_one_c_number || activeRow.order_number}</strong>
              </>
            )}
            <span>Источник</span><strong>{sourceDisplayLabel}</strong>
            <span>Остаток</span><strong>{qty(activeRow.remaining_qty)} {activeRow.unit}</strong>
            <span>Кол-во запуска</span>
            {activeRow.work_item_id != null ? (
              <span className="batchEditCell">
                <input
                  type="number"
                  aria-label="Количество запуска"
                  min={0.001}
                  max={activeRow.launchable_qty ?? activeRow.quantity}
                  step={1}
                  value={launchValue}
                  onChange={(event) => {
                    const next = event.target.value
                    setLaunchValue(next)
                    const value = Number(next)
                    const max = activeRow.launchable_qty ?? activeRow.quantity
                    if (next !== '' && Number.isFinite(value) && value > 0 && value <= max) {
                      onLaunchQuantityChange(value)
                    }
                  }}
                  onBlur={commitLaunchQuantity}
                  onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }}
                />
                {activeRow.unit && <span className="batchUnit">{activeRow.unit}</span>}
              </span>
            ) : (
              <strong>{qty(activeRow.quantity)} {activeRow.unit}</strong>
            )}
            <span>Статус</span><strong>{activeStatusLabel}</strong>
            <span>Обеспечение</span>
            <strong>
              <span className={`pill ${activeCoverageStatus}`}>
                {activeCoverageLabel}
              </span>
            </strong>
            <span>Участок</span><strong>{activeRow.workshop_name || activeRow.stage_name || '—'}</strong>
            <span>Оптим. партия</span>
            <span className="batchEditCell">
              <input
                type="number"
                min={0}
                step={1}
                value={batchValue}
                disabled={batchSaving}
                placeholder="не задана"
                className={batchError ? 'inputError' : ''}
                onChange={(e) => { setBatchValue(e.target.value); setBatchError('') }}
                onBlur={() => void handleBatchSave()}
                onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
              />
              {activeRow.unit && <span className="batchUnit">{activeRow.unit}</span>}
              {batchSaving && <span className="batchHint">...</span>}
              {batchError && <span className="batchHint error">{batchError}</span>}
            </span>
          </div>
          {activeRow.paint_weld_chain?.counterpart_product_id && (
            <div className="mrpCoverageBlock">
              <div className="mrpCoverageTitle">Агрегированная цепочка «сварка → окраска»</div>
              <div className="detailGrid">
                <span>Окраска</span><strong>{activeOrderNumber()} · {activeRow.item_name}</strong>
                <span>Сварка</span>
                <strong>
                  {activeRow.paint_weld_chain.counterpart_order_prodplan_number
                    || activeRow.paint_weld_chain.counterpart_order_number} · {activeRow.paint_weld_chain.counterpart_item_name}
                </strong>
                <span>Остаток сварки</span>
                <strong>
                  {qty(activeRow.paint_weld_chain.counterpart_remaining_qty)} {activeRow.paint_weld_chain.counterpart_unit}
                </strong>
                <span>Действие</span><strong>Совместный выпуск и один сдельный наряд</strong>
              </div>
            </div>
          )}
          <ItemLedgerSummaryBlock itemId={activeRow.item_id} unit={activeRow.unit} />
          {hasShelfLaunchData && (
            <div className="shelfLaunchBlock">
              <div className="shelfLaunchTitle">
                <span>Запуск с полки</span>
                <span className={`planningBadge ${activeRow?.launch_source || 'mrp'}`}>
                  {activeRow?.launch_source || 'MRP'}
                </span>
              </div>
              <div className="detailGrid">
                <span>Источник запуска</span><strong>{activeRow?.launch_source || '—'}</strong>
                <span>Полка</span><strong>{activeRow?.shelf_warehouse_ref1c || '—'}</strong>
                <span>Требуется с полки</span><strong>{shelfQty(activeRow?.shelf_pull_qty)}</strong>
                <span>Материализовано на полке</span><strong>{shelfQty(activeRow?.shelf_materialized_qty)}</strong>
                <span>Дата запуска с полки</span><strong>{dateRu(activeRow?.shelf_latest_start_date) || '—'}</strong>
              </div>
            </div>
          )}
          {hasMrpCoverage && (
            <div className="mrpCoverageBlock">
              <div className="mrpCoverageTitle">MRP потребность #{activeRow.source_mrp_requirement_id}</div>
              <div className="mrpCoverageGrid">
                <span>Потребность</span><strong>{qty(activeRow.mrp_req_net_qty)} {activeRow.unit}</strong>
                <span>Закрыто</span><strong>{qty(activeRow.mrp_req_covered_qty)} {activeRow.unit}</strong>
                <span>Остаток</span>
                <strong className={mrpRemaining == null ? '' : mrpRemaining > 0.001 ? 'mrpRemainingWarn' : 'mrpRemainingOk'}>
                  {mrpRemaining == null ? '—' : qty(mrpRemaining)} {activeRow.unit}
                </strong>
              </div>
              {mrpRemaining != null && mrpRemaining <= 0.001 && (
                <div className="mrpCoveredBadge">Потребность закрыта полностью</div>
              )}
            </div>
          )}
          <div className="detailActions">
            <button onClick={onLoadMaterials}>Повторить загрузку</button>
            <button onClick={onPrint} disabled={!activeRow?.product_id}>Печать листа</button>
            {activeRow?.product_id && (
              <button
                onClick={onProduce}
                disabled={Boolean(activeRow.selection_disabled_reason)}
                title={activeRow.selection_disabled_reason
                  || 'Создать и провести СборкаЗапасов и СдельныйНаряд в 1С; факт принять после read-back'}
              >
                {activeRow.paint_weld_chain ? 'Произвести цепочку' : 'Произвести строку'}
              </button>
            )}
            {activeRow?.product_id && (
              <button
                onClick={onReturnLeftovers}
                title="Запросить возврат; backend проверит принятый выпуск, исходящую выдачу и фактический остаток"
              >
                Вернуть остатки
              </button>
            )}
          </div>
          <h3>
            {materials?.coverage_basis_item_name
              ? `Показаны компоненты сварной детали: ${materials.coverage_basis_item_name}`
              : 'Комплектующие'}
            {activeRow.work_item_id != null && materials?.qty != null
              ? ` на ${qty(materials.qty)} ${activeRow.unit || ''}`
              : ''}
          </h3>
          <div className="materialsList">
            {(materials?.components ?? []).map((m) => (
              <div className={`materialRow ${m.availability_status || m.coverage_status || 'unknown'}`} key={m.component_item_id}>
                <div>
                  <strong>{m.item_name}</strong>
                  <span>{m.item_article || m.item_code}</span>
                  <em className="materialEta">{expectedLine(m)}</em>
                  {reservationLine(m) && <em className="materialReserve">{reservationLine(m)}</em>}
                  {!!m.reserved_orders?.length && (
                    <em className="materialReserve">
                      {m.reserved_orders.slice(0, 3).map((r) => (
                        <span key={`${m.component_item_id}-${r.product_id}`}>
                          в резерве {r.order_number || `#${r.product_id}`}: {qty(r.reserved_qty)} {m.unit || ''}
                        </span>
                      ))}
                      {m.reserved_orders.length > 3 && <span>ещё {m.reserved_orders.length - 3}</span>}
                    </em>
                  )}
                </div>
                <div className="matNums">
                  <span>нужно {qty(m.required_qty)}</span>
                  <span>есть {qty(m.available_qty)}</span>
                  {(m.missing_qty ?? 0) > 0 && <span className="matMissing">нет {qty(m.missing_qty)}</span>}
                </div>
                <span className={`miniPill ${m.availability_status || m.coverage_status || 'unknown'}`}>
                  {m.coverage_label || coverageLabels[String(m.availability_status || m.coverage_status || '')] || m.availability_status || m.coverage_status}
                </span>
              </div>
            ))}
            {!materials?.components?.length && <div className="emptyDetail">Материалы не загружены</div>}
          </div>
        </>
      ) : (
        <div className="emptyDetail">Выберите строку</div>
      )}
    </aside>
  )
}
