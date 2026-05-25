import { useEffect, useState } from 'react'
import { productionStatusLabel, type MaterialsResponse, type OrderRow } from '../../../domain/productionControl'
import { qty } from '../../../lib/format'

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

  const canEditQuantity = activeRow?.source === 'mrp' && !activeRow?.source_mrp_allocation_key?.startsWith('1C')
  const hasMrpCoverage = activeRow?.source_mrp_requirement_id != null && activeRow?.mrp_req_net_qty != null
  const mrpRemaining = activeRow?.mrp_req_remaining_qty ?? 0
  const canFillRemaining = hasMrpCoverage && mrpRemaining > 0.001 && !!activeRow?.source_run_id

  return (
    <aside className="detailPane">
      <h2>Карточка строки</h2>
      {activeRow ? (
        <>
          <div className="detailTitle">{activeRow.item_name}</div>
          <div className="detailMeta">{activeRow.item_article || activeRow.item_code}</div>
          <div className="detailGrid">
            <span>Заказ</span><strong>{activeRow.order_number}</strong>
            <span>Источник</span><strong>{activeRow.source_mrp_requirement_id ? `MRP req #${activeRow.source_mrp_requirement_id}` : activeRow.source || '1C'}</strong>
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
            <span>Обеспечение</span><strong>{activeRow.coverage_label || coverageLabels[String(activeRow.coverage_status || '')]}</strong>
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
              <div className="materialRow" key={m.component_item_id}>
                <div>
                  <strong>{m.item_name}</strong>
                  <span>{m.item_article || m.item_code}</span>
                </div>
                <div className="matNums">
                  <span>нужно {qty(m.required_qty)}</span>
                  <span>есть {qty(m.available_qty)}</span>
                </div>
                <span className={`miniPill ${m.availability_status || 'unknown'}`}>{coverageLabels[String(m.availability_status || '')] || m.availability_status}</span>
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
