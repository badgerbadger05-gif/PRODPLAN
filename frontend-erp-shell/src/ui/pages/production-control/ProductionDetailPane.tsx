import { useEffect, useState } from 'react'
import { productionStatusLabel, type MaterialsResponse, type OrderRow } from '../../../domain/productionControl'
import { dateRu, qty } from '../../../lib/format'

type Props = {
  activeRow: OrderRow | null
  materials: MaterialsResponse | null
  coverageLabels: Record<string, string>
  onLoadMaterials: () => void
  onPrint: () => void
  onOptimalBatchSave: (itemId: number, value: number | null) => Promise<void>
  onQuantitySave: (productId: number, value: number) => Promise<void>
  onFillRemaining: (sourceRunId: number, requirementId: number) => Promise<void>
}

export function ProductionDetailPane({
  activeRow,
  materials,
  coverageLabels,
  onLoadMaterials,
  onPrint,
  onOptimalBatchSave,
  onQuantitySave,
  onFillRemaining,
}: Props) {
  const [batchValue, setBatchValue] = useState('')
  const [quantityValue, setQuantityValue] = useState('')
  const [batchSaving, setBatchSaving] = useState(false)
  const [quantitySaving, setQuantitySaving] = useState(false)
  const [fillSaving, setFillSaving] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [quantityError, setQuantityError] = useState('')
  const [fillError, setFillError] = useState('')

  useEffect(() => {
    setBatchValue(activeRow?.optimal_batch != null ? String(activeRow.optimal_batch) : '')
    setQuantityValue(activeRow?.quantity != null ? String(activeRow.quantity) : '')
    setBatchError('')
    setQuantityError('')
    setFillError('')
  }, [activeRow?.product_id, activeRow?.optimal_batch, activeRow?.quantity])

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

  async function handleQuantitySave() {
    if (!activeRow?.product_id) return
    const value = Number(quantityValue.trim() || 0)
    if (Number.isNaN(value) || value < 0) {
      setQuantityError('Некорректное значение')
      return
    }
    setQuantityError('')
    setQuantitySaving(true)
    try {
      await onQuantitySave(activeRow.product_id, value)
    } catch {
      setQuantityError('Ошибка сохранения')
    } finally {
      setQuantitySaving(false)
    }
  }

  async function handleFillRemaining() {
    if (!activeRow?.source_run_id || !activeRow?.source_mrp_requirement_id) return
    setFillError('')
    setFillSaving(true)
    try {
      await onFillRemaining(activeRow.source_run_id, activeRow.source_mrp_requirement_id)
    } catch {
      setFillError('Ошибка создания заказов')
    } finally {
      setFillSaving(false)
    }
  }

  const rowSource = activeRow?.order_source || activeRow?.source
  const canEditQuantity = rowSource === 'mrp' && !activeRow?.source_mrp_allocation_key?.startsWith('1C')
  const hasMrpCoverage = activeRow?.source_mrp_requirement_id != null && activeRow?.mrp_req_net_qty != null
  const mrpRemaining = activeRow?.mrp_req_remaining_qty ?? 0
  const canFillRemaining = hasMrpCoverage && mrpRemaining > 0.001 && !!activeRow?.source_run_id
  const canUseMaterialCoverage = !activeRow?.issue_status || activeRow.issue_status === 'not_requested'
  const activeCoverageStatus = (canUseMaterialCoverage ? materials?.coverage_status : '')
    || activeRow?.coverage_status
    || activeRow?.status
    || 'unknown'
  const activeCoverageLabel = (canUseMaterialCoverage ? materials?.coverage_label : '')
    || activeRow?.coverage_label
    || coverageLabels[String(activeCoverageStatus)]
    || activeCoverageStatus
  const planSourceLabel = activeRow?.source_plan_name
    || (activeRow?.source_plan_id ? `План #${activeRow.source_plan_id}` : '')
  const dbrPlanning = activeRow?.planning?.contour === 'dbr_feeder' ? activeRow.planning : null
  const sourceDisplayLabel = dbrPlanning ? 'DBR · очередь мехцеха' : (planSourceLabel || rowSource || '1C')

  function queueStateLabel(state?: string | null) {
    if (state === 'ready') return 'Готов к запуску'
    if (state === 'blocked') return 'Заблокирован материалами'
    if (state === 'not_due') return 'Срок запуска не наступил'
    if (state === 'diagnostic') return 'Требует разбора'
    return state || '—'
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
    if ((m.missing_qty ?? 0) > 0) return 'В заказах нет'
    return 'На складе'
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
            <span className="batchEditCell">
              <input
                type="number"
                min={0}
                step={1}
                value={quantityValue}
                disabled={!canEditQuantity || quantitySaving}
                className={quantityError ? 'inputError' : ''}
                onChange={(e) => { setQuantityValue(e.target.value); setQuantityError('') }}
                onBlur={() => void handleQuantitySave()}
                onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
              />
              {activeRow.unit && <span className="batchUnit">{activeRow.unit}</span>}
              {quantitySaving && <span className="batchHint">...</span>}
              {quantityError && <span className="batchHint error">{quantityError}</span>}
            </span>
            <span>Статус</span><strong>{productionStatusLabel(activeRow.status)}</strong>
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
          {dbrPlanning && (
            <div className="dbrPlanningBlock">
              <div className="dbrPlanningTitle">
                <span>Планирование DBR</span>
                <span className={`planningBadge ${dbrPlanning.zone || 'dbr'}`}>
                  {dbrPlanning.zone === 'red' ? 'Красная зона' : dbrPlanning.zone === 'yellow' ? 'Жёлтая зона' : dbrPlanning.zone === 'green' ? 'Зелёная зона' : 'DBR'}
                </span>
              </div>
              <div className="detailGrid">
                <span>Сигнал</span><strong>#{activeRow.source_dbr_signal_id || dbrPlanning.source_id || '—'}</strong>
                <span>Тип</span><strong>{dbrPlanning.signal_type || '—'}</strong>
                <span>Приоритет</span><strong>{dbrPlanning.priority ?? '—'}</strong>
                <span>Требуется</span><strong>{dateRu(dbrPlanning.required_date) || '—'}</strong>
                <span>Слот барабана</span><strong>{dbrPlanning.slot_id ? `#${dbrPlanning.slot_id}` : '—'}</strong>
                <span>Состояние очереди</span><strong>{queueStateLabel(dbrPlanning.queue_state)}</strong>
                {dbrPlanning.reason && (
                  <>
                    <span>Причина</span><strong>{dbrPlanning.reason}</strong>
                  </>
                )}
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
                <strong className={mrpRemaining > 0.001 ? 'mrpRemainingWarn' : 'mrpRemainingOk'}>
                  {qty(mrpRemaining)} {activeRow.unit}
                </strong>
              </div>
              {canFillRemaining && (
                <div className="mrpCoverageActions">
                  <button className="primary" disabled={fillSaving} onClick={() => void handleFillRemaining()}>
                    {fillSaving ? 'Создаём...' : `Досоздать ${qty(mrpRemaining)} ${activeRow.unit}`}
                  </button>
                  {fillError && <span className="batchHint error">{fillError}</span>}
                </div>
              )}
              {!canFillRemaining && mrpRemaining <= 0.001 && (
                <div className="mrpCoveredBadge">Потребность закрыта полностью</div>
              )}
            </div>
          )}
          <div className="detailActions">
            <button onClick={onLoadMaterials}>Обновить материалы</button>
            <button onClick={onPrint}>Печать листа</button>
          </div>
          <h3>Комплектующие</h3>
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
