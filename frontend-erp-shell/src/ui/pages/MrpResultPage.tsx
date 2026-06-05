import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import type { MrpCapacityRow, MrpProductionRow, MrpPurchaseRow, MrpReworkRow, MrpSummary } from '../../domain/planning'
import { planningStatusLabel } from '../../domain/planning'
import { downloadBase64File } from '../../lib/download'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import {
  createProductionControlOrdersFromMrp,
  exportPlanningResultProduction,
  exportPlanningResultPurchases,
  exportPlanningResultRework,
  exportPurchasesTo1C,
  getPlanningResultCapacity,
  getPlanningResultProduction,
  getPlanningResultPurchases,
  getPlanningResultRework,
  getPlanningRunSummary,
} from '../../services/planning'
import { getPeriodPlanMatrix } from '../../services/periodPlan'
import { DocumentWindow } from '../layout/DocumentWindow'
import { RootProductFilterDialog, rootProductLabel, type RootProductOption } from '../RootProductFilterDialog'
import { StatusBar } from '../layout/StatusBar'

type Tab = 'production' | 'purchases' | 'rework' | 'capacity'

const limit = 200

function emptyTabFlags(): Record<Tab, boolean> {
  return { production: false, purchases: false, rework: false, capacity: false }
}

function emptyTabOffsets(): Record<Tab, number> {
  return { production: 0, purchases: 0, rework: 0, capacity: 0 }
}

function parseTab(value: string | null): Tab | null {
  if (value === 'production' || value === 'purchases' || value === 'rework' || value === 'capacity') return value
  return null
}

function parseId(value: string | null) {
  const id = Number(value)
  return Number.isFinite(id) && id > 0 ? id : null
}

function supplierDisplayName(row: MrpPurchaseRow) {
  return (row.supplier_name || '').trim() || 'Без наименования'
}

function supplierFilterKey(row: MrpPurchaseRow) {
  return (row.supplier_name || '').trim() ? (row.supplier_ref1c || row.supplier_name || '') : '__missing_supplier_name'
}

function ForecastShift({ forecast }: { forecast?: { forecast_date?: string | null; forecast_shift_days?: number | null; forecast_reason?: string | null } | null }) {
  if (!forecast || forecast.forecast_shift_days === null || forecast.forecast_shift_days === undefined) return null
  const days = Number(forecast.forecast_shift_days)
  if (!Number.isFinite(days) || days === 0) return null
  const cls = days > 5 ? 'late' : days > 0 ? 'warn' : 'early'
  const label = `${days > 0 ? '+' : ''}${days} дн`
  const dateText = forecast.forecast_date ? dateRu(forecast.forecast_date).slice(0, 5) : ''
  const title = [forecast.forecast_reason, forecast.forecast_date ? `прогноз ${dateRu(forecast.forecast_date)}` : null].filter(Boolean).join(' · ')
  return <span className={`forecastShift ${cls}`} title={title}>{label}{dateText ? ` · ${dateText}` : ''}</span>
}

export function MrpResultPage() {
  const { runId: runIdParam } = useParams<{ runId: string }>()
  const runId = Number(runIdParam)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryTab = parseTab(searchParams.get('tab'))
  const highlightedProductionId = parseId(searchParams.get('planned_order_id'))
  const highlightedPurchaseId = parseId(searchParams.get('purchase_id'))
  const highlightedReworkId = parseId(searchParams.get('rework_id'))
  const [summary, setSummary] = useState<MrpSummary | null>(null)
  const [tab, setTab] = useState<Tab>(() => queryTab ?? 'production')
  const [loadedTabs, setLoadedTabs] = useState<Record<Tab, boolean>>(() => emptyTabFlags())
  const [offsets, setOffsets] = useState<Record<Tab, number>>(() => emptyTabOffsets())
  const [productionRows, setProductionRows] = useState<MrpProductionRow[]>([])
  const [purchaseRows, setPurchaseRows] = useState<MrpPurchaseRow[]>([])
  const [reworkRows, setReworkRows] = useState<MrpReworkRow[]>([])
  const [capacityRows, setCapacityRows] = useState<MrpCapacityRow[]>([])
  const [productionTotal, setProductionTotal] = useState(0)
  const [purchaseTotal, setPurchaseTotal] = useState(0)
  const [reworkTotal, setReworkTotal] = useState(0)
  const [capacityTotal, setCapacityTotal] = useState(0)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [draftDateFrom, setDraftDateFrom] = useState('')
  const [draftDateTo, setDraftDateTo] = useState('')
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [selectedProductionIds, setSelectedProductionIds] = useState<Set<number>>(new Set())
  const [selectedPurchaseIds, setSelectedPurchaseIds] = useState<Set<number>>(new Set())
  const [purchaseSupplierFilter, setPurchaseSupplierFilter] = useState('')
  const [purchaseCategoryFilter, setPurchaseCategoryFilter] = useState('')
  const [rootItemId, setRootItemId] = useState<number | null>(null)
  const [rootOptions, setRootOptions] = useState<RootProductOption[]>([])
  const [rootDialogOpen, setRootDialogOpen] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const loadSeq = useRef(0)

  const purchaseSupplierOptions = useMemo(() => {
    const map = new Map<string, string>()
    purchaseRows.forEach((row) => {
      const key = supplierFilterKey(row)
      if (!key) return
      map.set(key, supplierDisplayName(row))
    })
    return Array.from(map.entries()).map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label, 'ru'))
  }, [purchaseRows])
  const purchaseCategoryOptions = useMemo(() => {
    const map = new Map<string, string>()
    purchaseRows.forEach((row) => {
      const key = row.category_id !== null && row.category_id !== undefined
        ? String(row.category_id)
        : (row.category_ref1c || row.category_name || '')
      if (!key) return
      map.set(key, row.category_name || 'Без товарной группы')
    })
    return Array.from(map.entries()).map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label, 'ru'))
  }, [purchaseRows])
  const filteredPurchaseRows = useMemo(() => (
    purchaseRows.filter((row) => {
      const supplierKey = supplierFilterKey(row)
      const categoryKey = row.category_id !== null && row.category_id !== undefined
        ? String(row.category_id)
        : (row.category_ref1c || row.category_name || '')
      if (purchaseSupplierFilter && supplierKey !== purchaseSupplierFilter) return false
      if (purchaseCategoryFilter && categoryKey !== purchaseCategoryFilter) return false
      return true
    })
  ), [purchaseCategoryFilter, purchaseRows, purchaseSupplierFilter])
  const activeOffset = offsets[tab]
  const activeTotal = tab === 'production' ? productionTotal : tab === 'purchases' ? purchaseTotal : tab === 'rework' ? reworkTotal : capacityTotal
  const activeRowsLength = tab === 'production' ? productionRows.length : tab === 'purchases' ? purchaseRows.length : tab === 'rework' ? reworkRows.length : capacityRows.length
  const activeVisibleFrom = activeTotal && activeRowsLength ? activeOffset + 1 : 0
  const activeVisibleTo = activeTotal && activeRowsLength ? Math.min(activeOffset + activeRowsLength, activeTotal) : 0
  const selectedCount = tab === 'production' ? selectedProductionIds.size : tab === 'purchases' ? selectedPurchaseIds.size : 0

  const totals = useMemo(() => ({
    productionQty: productionRows.reduce((sum, row) => sum + Number(row.qty || 0), 0),
    purchaseQty: purchaseRows.reduce((sum, row) => sum + Number(row.qty || 0), 0),
    reworkQty: reworkRows.reduce((sum, row) => sum + Number(row.qty || 0), 0),
    overloadHours: capacityRows.reduce((sum, row) => sum + Number(row.overload_hours || 0), 0),
  }), [productionRows, purchaseRows, reworkRows, capacityRows])

  useEffect(() => {
    if (queryTab) setTab(queryTab)
  }, [queryTab])

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await getPlanningRunSummary(runId))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [runId])

  const loadTab = useCallback(async (targetTab: Tab, nextOffset: number) => {
    const seq = ++loadSeq.current
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const params = { date_from: dateFrom || undefined, date_to: dateTo || undefined, root_item_id: rootItemId, limit, offset: nextOffset }
      if (targetTab === 'production') {
        const data = await getPlanningResultProduction(runId, params)
        if (seq !== loadSeq.current) return
        setProductionRows(data.rows ?? [])
        setProductionTotal(data.total ?? 0)
      } else if (targetTab === 'purchases') {
        const data = await getPlanningResultPurchases(runId, params)
        if (seq !== loadSeq.current) return
        setPurchaseRows(data.rows ?? [])
        setPurchaseTotal(data.total ?? 0)
      } else if (targetTab === 'rework') {
        const data = await getPlanningResultRework(runId, params)
        if (seq !== loadSeq.current) return
        setReworkRows(data.rows ?? [])
        setReworkTotal(data.total ?? 0)
      } else {
        const data = await getPlanningResultCapacity(runId, params)
        if (seq !== loadSeq.current) return
        setCapacityRows(data.rows ?? [])
        setCapacityTotal(data.total ?? 0)
      }
      setOffsets((prev) => ({ ...prev, [targetTab]: nextOffset }))
      setLoadedTabs((prev) => ({ ...prev, [targetTab]: true }))
    } catch (e) {
      if (seq === loadSeq.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [dateFrom, dateTo, rootItemId, runId])

  const invalidateTabs = useCallback(() => {
    loadSeq.current += 1
    setLoadedTabs(emptyTabFlags())
    setOffsets(emptyTabOffsets())
    setProductionRows([])
    setPurchaseRows([])
    setReworkRows([])
    setCapacityRows([])
    setProductionTotal(0)
    setPurchaseTotal(0)
    setReworkTotal(0)
    setCapacityTotal(0)
    setSelectedProductionIds(new Set())
    setSelectedPurchaseIds(new Set())
    setPurchaseSupplierFilter('')
    setPurchaseCategoryFilter('')
  }, [])

  const refreshActiveTab = useCallback(() => {
    void loadTab(tab, offsets[tab])
  }, [loadTab, offsets, tab])

  useEffect(() => {
    void loadSummary()
  }, [loadSummary])

  useEffect(() => {
    if (!loadedTabs[tab]) void loadTab(tab, offsets[tab])
  }, [loadedTabs, loadTab, offsets, tab])

  useEffect(() => {
    let cancelled = false
    async function loadRootOptions(planId: number) {
      try {
        const data = await getPeriodPlanMatrix(planId)
        if (cancelled) return
        setRootOptions((data.rows ?? []).map((row) => ({
          item_id: row.item_id,
          item_name: row.item_name,
          item_article: row.item_article,
          item_code: row.item_code,
        })))
      } catch {
        if (!cancelled) setRootOptions([])
      }
    }
    const planId = summary?.run?.source_plan_id
    if (planId) void loadRootOptions(planId)
    else setRootOptions([])
    return () => { cancelled = true }
  }, [summary?.run?.source_plan_id])

  async function exportActive(format: 'csv' | 'xlsx') {
    setExporting(true)
    setError('')
    try {
      const params = { format, date_from: dateFrom || undefined, date_to: dateTo || undefined, root_item_id: rootItemId }
      const response = tab === 'production'
        ? await exportPlanningResultProduction(runId, params)
        : tab === 'purchases'
          ? await exportPlanningResultPurchases(runId, params)
          : await exportPlanningResultRework(runId, params)
      downloadBase64File(response, `mrp_${tab}_${runId}.${format}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  async function createSelectedProductionOrders() {
    if (!selectedProductionIds.size) return
    setExporting(true)
    setError('')
    setMessage('')
    try {
      const result = await createProductionControlOrdersFromMrp({
        run_id: runId,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        planned_order_ids: Array.from(selectedProductionIds),
      })
      setSelectedProductionIds(new Set())
      await loadSummary()
      await loadTab('production', offsets.production)
      setMessage(formatActionResult('Создание заказов', result))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  async function exportSelectedPurchasesTo1C() {
    if (!selectedPurchaseIds.size) return
    setExporting(true)
    setError('')
    setMessage('')
    try {
      const result = await exportPurchasesTo1C(runId, {
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        purchase_ids: Array.from(selectedPurchaseIds),
      })
      setMessage(formatActionResult('Выгрузка закупок в 1С', result))
      setSelectedPurchaseIds(new Set())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  function applyDateFilters() {
    setDateFrom(draftDateFrom)
    setDateTo(draftDateTo)
    invalidateTabs()
  }

  function applyRootFilter(value: number | null) {
    setRootItemId(value)
    setRootDialogOpen(false)
    invalidateTabs()
  }

  function goPrev() {
    const nextOffset = Math.max(0, activeOffset - limit)
    setLoadedTabs((prev) => ({ ...prev, [tab]: false }))
    setOffsets((prev) => ({ ...prev, [tab]: nextOffset }))
  }

  function goNext() {
    const nextOffset = activeOffset + limit
    setLoadedTabs((prev) => ({ ...prev, [tab]: false }))
    setOffsets((prev) => ({ ...prev, [tab]: nextOffset }))
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">MRP / Результат прогона #{runId}</div>
        <div className="runBadge">{summary?.run?.status ? planningStatusLabel(summary.run.status) : 'Загрузка'}</div>
      </div>

      <DocumentWindow
        title={`Результаты MRP #${runId}`}
        subtitle="Производство, закупки и переработка в едином рабочем представлении"
        hotkeys="Esc Назад · F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={activeVisibleFrom}
            visibleTo={activeVisibleTo}
            total={activeTotal}
            selectedCount={selectedCount}
            canPrev={activeOffset > 0}
            canNext={activeOffset + activeRowsLength < activeTotal}
            onPrev={goPrev}
            onNext={goNext}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => navigate('/mrp-runs')}>К списку прогонов</button>
          <button onClick={() => { void loadSummary(); refreshActiveTab() }} disabled={loading}>Обновить</button>
          {tab !== 'capacity' && <button onClick={() => void exportActive('xlsx')} disabled={loading || exporting}>XLSX</button>}
          {tab === 'production' && <button className="primary" onClick={() => void createSelectedProductionOrders()} disabled={!selectedProductionIds.size || loading || exporting}>Создать заказы ({selectedProductionIds.size})</button>}
          {tab === 'purchases' && <button className="primary" onClick={() => void exportSelectedPurchasesTo1C()} disabled={!selectedPurchaseIds.size || loading || exporting}>Выгрузить в 1С ({selectedPurchaseIds.size})</button>}
          <div className="barSeparator" />
          <button onClick={() => setRootDialogOpen(true)}>Корневое изделие</button>
          <span className="toolbarText">{rootProductLabel(rootOptions, rootItemId)}</span>
          <div className="barSeparator" />
          <label className="inlineControl">
            <span>С</span>
            <input type="date" value={draftDateFrom} onChange={(e) => setDraftDateFrom(e.target.value)} />
          </label>
          <label className="inlineControl">
            <span>По</span>
            <input type="date" value={draftDateTo} onChange={(e) => setDraftDateTo(e.target.value)} />
          </label>
          <button className="filterBtn" onClick={applyDateFilters} disabled={loading}>Сформировать</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="mrpSummaryStrip">
          <Metric title="Старт" value={dateTimeRu(summary?.run?.started_at) || '—'} />
          <Metric title="Горизонт" value={`${qty(summary?.run?.horizon_days)} дн.`} />
          <Metric title="Производство" value={qty(summary?.counts?.production_orders ?? productionTotal)} hint={`${qty(totals.productionQty)} шт.`} />
          <Metric title="Закупки" value={qty(summary?.counts?.purchase_requests ?? purchaseTotal)} hint={`${qty(totals.purchaseQty)} шт.`} />
          <Metric title="Переработка" value={qty(summary?.counts?.rework_requests ?? reworkTotal)} hint={`${qty(totals.reworkQty)} шт.`} />
          <Metric title="Перегрузы" value={qty(summary?.capacity?.overloaded_buckets)} hint={`${qty(totals.overloadHours)} н/ч`} />
        </div>

        <div className="tabsBar">
          <button className={tab === 'production' ? 'activeTab' : ''} onClick={() => setTab('production')}>Производство</button>
          <button className={tab === 'purchases' ? 'activeTab' : ''} onClick={() => setTab('purchases')}>Закупки</button>
          <button className={tab === 'rework' ? 'activeTab' : ''} onClick={() => setTab('rework')}>Переработка</button>
          <button className={tab === 'capacity' ? 'activeTab' : ''} onClick={() => setTab('capacity')}>Мощности</button>
        </div>

        <div className="tablePane resultTablePane">
          {tab === 'production' && <ProductionResultTable rows={productionRows} selectedIds={selectedProductionIds} highlightedId={highlightedProductionId} onSelectedIdsChange={setSelectedProductionIds} />}
          {tab === 'purchases' && (
            <PurchaseResultTable
              rows={filteredPurchaseRows}
              selectedIds={selectedPurchaseIds}
              highlightedId={highlightedPurchaseId}
              supplierFilter={purchaseSupplierFilter}
              categoryFilter={purchaseCategoryFilter}
              supplierOptions={purchaseSupplierOptions}
              categoryOptions={purchaseCategoryOptions}
              onSupplierFilterChange={setPurchaseSupplierFilter}
              onCategoryFilterChange={setPurchaseCategoryFilter}
              onSelectedIdsChange={setSelectedPurchaseIds}
            />
          )}
          {tab === 'rework' && <ReworkResultTable rows={reworkRows} highlightedId={highlightedReworkId} />}
          {tab === 'capacity' && <CapacityResultTable rows={capacityRows} />}
        </div>
      </DocumentWindow>
      <RootProductFilterDialog
        open={rootDialogOpen}
        options={rootOptions}
        value={rootItemId}
        onApply={applyRootFilter}
        onClose={() => setRootDialogOpen(false)}
      />
    </main>
  )
}

function Metric({ title, value, hint }: { title: string; value: string; hint?: string }) {
  return (
    <div className="metricCell">
      <span>{title}</span>
      <strong>{value}</strong>
      {hint && <em>{hint}</em>}
    </div>
  )
}

function formatActionResult(title: string, result: Record<string, unknown>) {
  const created = numberValue(result.created ?? result.created_count ?? result.orders_created ?? result.exported ?? result.exported_count)
  const existing = numberValue(result.existing ?? result.existing_count ?? result.orders_existing)
  const skipped = countValue(result.skipped ?? result.skipped_count ?? result.skipped_rows)
  const errors = countValue(result.errors ?? result.error_count)
  const parts = [`${title}: выполнено`]
  if (created) parts.push(`новых ${created}`)
  if (existing) parts.push(`уже было ${existing}`)
  if (skipped) parts.push(`пропущено ${skipped}`)
  if (errors) parts.push(`ошибок ${errors}`)
  if (parts.length > 1) return parts.join(', ')
  return `${title}: ${JSON.stringify(result).slice(0, 220)}`
}

function numberValue(value: unknown) {
  const number = Number(value ?? 0)
  return Number.isFinite(number) ? number : 0
}

function countValue(value: unknown) {
  if (Array.isArray(value)) return value.length
  return numberValue(value)
}

function toggleMany(set: Set<number>, ids: number[], checked: boolean) {
  const next = new Set(set)
  ids.forEach((id) => {
    if (checked) next.add(id)
    else next.delete(id)
  })
  return next
}

function rowOrderIds(row: MrpProductionRow) {
  return row.source_order_ids?.length ? row.source_order_ids : [row.order_id]
}

function isProductionRowSelectable(row: MrpProductionRow) {
  return Number(row.qty || 0) > 0
}

function rowPurchaseIds(row: MrpPurchaseRow) {
  return row.source_purchase_ids?.length ? row.source_purchase_ids : [row.purchase_id]
}

function ProductionResultTable({ rows, selectedIds, highlightedId, onSelectedIdsChange }: {
  rows: MrpProductionRow[]
  selectedIds: Set<number>
  highlightedId: number | null
  onSelectedIdsChange: (ids: Set<number>) => void
}) {
  const visibleIds = rows.filter(isProductionRowSelectable).flatMap(rowOrderIds)
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id))

  return (
    <table className="journalTable resultTable">
      <thead>
        <tr>
          <th className="checkCol">
            <input
              type="checkbox"
              checked={allVisibleSelected}
              disabled={!visibleIds.length}
              onChange={(e) => onSelectedIdsChange(toggleMany(selectedIds, visibleIds, e.target.checked))}
              aria-label="Выбрать все видимые производственные заказы"
            />
          </th>
          <th>Изделие</th>
          <th>Кол-во</th>
          <th>Потребность</th>
          <th>Старт</th>
          <th>Финиш</th>
          <th>Участок</th>
          <th>Н/ч</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.order_id} className={highlightedId && rowOrderIds(row).includes(highlightedId) ? 'activeRow' : undefined}>
            <td className="checkCol">
              <input
                type="checkbox"
                checked={rowOrderIds(row).every((id) => selectedIds.has(id))}
                disabled={!isProductionRowSelectable(row)}
                onChange={(e) => onSelectedIdsChange(toggleMany(selectedIds, rowOrderIds(row), e.target.checked))}
                aria-label={`Выбрать ${row.item_name || row.item_article || row.order_id}`}
              />
            </td>
            <td className="itemCell">
              <strong>{row.item_name || `Номенклатура #${row.item_id}`}</strong>
              <span>{row.item_article || ''} {row.badge || ''}</span>
            </td>
            <td className="numCell"><strong>{qty(row.qty)}</strong><span>{row.unit || ''}</span></td>
            <td>{dateRu(row.need_date) || '—'}</td>
            <td>{dateRu(row.start_date) || '—'}</td>
            <td>{dateRu(row.finish_date) || '—'}<ForecastShift forecast={row} /></td>
            <td>{row.main_area_name || row.main_stage_name || '—'}</td>
            <td className="numCell"><strong>{qty(row.norm_hours_total)}</strong><span>н/ч</span></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function PurchaseResultTable({
  rows,
  selectedIds,
  highlightedId,
  supplierFilter,
  categoryFilter,
  supplierOptions,
  categoryOptions,
  onSupplierFilterChange,
  onCategoryFilterChange,
  onSelectedIdsChange,
}: {
  rows: MrpPurchaseRow[]
  selectedIds: Set<number>
  highlightedId: number | null
  supplierFilter: string
  categoryFilter: string
  supplierOptions: Array<{ value: string; label: string }>
  categoryOptions: Array<{ value: string; label: string }>
  onSupplierFilterChange: (value: string) => void
  onCategoryFilterChange: (value: string) => void
  onSelectedIdsChange: (ids: Set<number>) => void
}) {
  const visibleIds = rows.flatMap(rowPurchaseIds)
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id))

  return (
    <table className="journalTable resultTable">
      <thead>
        <tr>
          <th className="checkCol">
            <input
              type="checkbox"
              checked={allVisibleSelected}
              disabled={!visibleIds.length}
              onChange={(e) => onSelectedIdsChange(toggleMany(selectedIds, visibleIds, e.target.checked))}
              aria-label="Выбрать все видимые заявки на закупку"
            />
          </th>
          <th>Номенклатура</th>
          <th>Поставщик</th>
          <th>Категория</th>
          <th>К заказу</th>
          <th>Потребность</th>
          <th>Заказать до</th>
          <th>Срок пост.</th>
          <th>Покрыто</th>
          <th>Примечание</th>
        </tr>
        <tr>
          <th />
          <th />
          <th>
            <select
              className="tableFilterSelect"
              value={supplierFilter}
              onChange={(e) => onSupplierFilterChange(e.target.value)}
              aria-label="Фильтр по поставщику"
            >
              <option value="">Все</option>
              {supplierOptions.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </th>
          <th>
            <select
              className="tableFilterSelect"
              value={categoryFilter}
              onChange={(e) => onCategoryFilterChange(e.target.value)}
              aria-label="Фильтр по категории"
            >
              <option value="">Все</option>
              {categoryOptions.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </th>
          <th />
          <th />
          <th />
          <th />
          <th />
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const covered = Number(row.supplier_covered_qty ?? 0)
          const requested = Number(row.requested_qty ?? row.qty)
          const coveragePct = requested > 0 ? Math.min(100, Math.round((covered / requested) * 100)) : 0
          const coverageLabel = covered > 0
            ? `${qty(covered)} / ${qty(requested)} ${row.unit || ''} (${coveragePct}%)`
            : '—'
          return (
          <tr key={row.purchase_id} className={highlightedId && rowPurchaseIds(row).includes(highlightedId) ? 'activeRow' : undefined}>
            <td className="checkCol">
              <input
                type="checkbox"
                checked={rowPurchaseIds(row).every((id) => selectedIds.has(id))}
                onChange={(e) => onSelectedIdsChange(toggleMany(selectedIds, rowPurchaseIds(row), e.target.checked))}
                aria-label={`Выбрать ${row.item_name || row.item_article || row.purchase_id}`}
              />
            </td>
            <td className="itemCell">
              <strong>{row.item_name || `Номенклатура #${row.item_id}`}</strong>
              <span>{row.item_article || ''}</span>
            </td>
            <td className="itemCell">
              <strong title={row.supplier_ref1c || undefined}>{supplierDisplayName(row)}</strong>
            </td>
            <td className="itemCell">
              <strong>{row.category_name || 'Без товарной группы'}</strong>
            </td>
            <td className="numCell"><strong>{qty(row.qty)}</strong><span>{row.unit || ''}</span></td>
            <td>{dateRu(row.need_date) || '—'}</td>
            <td>{dateRu(row.order_date) || '—'}</td>
            <td className="numCell"><strong>{Number(row.lead_time_days || 0) || '—'}</strong>{Number(row.lead_time_days || 0) > 0 && <span>дн.</span>}</td>
            <td className="numCell" title={`Покрыто активными заказами поставщику: ${coverageLabel}`} style={{ color: covered > 0 ? (coveragePct >= 100 ? 'var(--color-success, green)' : 'var(--color-warning, orange)') : undefined }}>
              {covered > 0 ? coverageLabel : '—'}
            </td>
            <td>{row.badge || (row.late_supplier_order ? 'Покрыто заказом, но с опозданием' : '')}</td>
          </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function ReworkResultTable({ rows, highlightedId }: { rows: MrpReworkRow[]; highlightedId: number | null }) {
  return (
    <table className="journalTable resultTable">
      <thead>
        <tr>
          <th>Номенклатура</th>
          <th>Потребность</th>
          <th>К плану</th>
          <th>Запуск</th>
          <th>Дата заказа</th>
          <th>Спецификация</th>
          <th>Ограничение</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.rework_id} className={highlightedId === row.rework_id ? 'activeRow' : undefined}>
            <td className="itemCell">
              <strong>{row.item_name || `Номенклатура #${row.item_id}`}</strong>
              <span>{row.item_article || ''} {row.badge || ''}</span>
            </td>
            <td className="numCell"><strong>{qty(row.requested_qty)}</strong><span>{row.unit || ''}</span></td>
            <td className="numCell"><strong>{qty(row.planned_qty)}</strong><span>{row.unit || ''}</span></td>
            <td className="numCell"><strong>{qty(row.qty)}</strong><span>{row.unit || ''}</span></td>
            <td>{dateRu(row.order_date) || dateRu(row.need_date) || '—'}</td>
            <td className="itemCell">
              <strong>{row.spec_name || '—'}</strong>
              <span>{row.spec_code || ''}</span>
            </td>
            <td>
              {row.component_blocked && <span className="pill shortage">Блок</span>}
              {!row.component_blocked && row.component_partial && <span className="pill partial">Частично</span>}
              {!row.component_blocked && !row.component_partial && <span className="pill ready">ОК</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function CapacityResultTable({ rows }: { rows: MrpCapacityRow[] }) {
  return (
    <table className="journalTable resultTable">
      <thead>
        <tr>
          <th>Участок</th>
          <th>Дата</th>
          <th>План, н/ч</th>
          <th>Доступно, н/ч</th>
          <th>Перегруз, н/ч</th>
          <th>Статус</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={`${row.area_id}-${row.bucket_date || index}`}>
            <td className="itemCell">
              <strong>Участок #{row.area_id}</strong>
              <span>производственная мощность</span>
            </td>
            <td>{dateRu(row.bucket_date) || '—'}</td>
            <td className="numCell"><strong>{qty(row.hours_planned)}</strong><span>н/ч</span></td>
            <td className="numCell"><strong>{qty(row.hours_available)}</strong><span>н/ч</span></td>
            <td className="numCell"><strong>{qty(row.overload_hours)}</strong><span>н/ч</span></td>
            <td>
              {Number(row.overload_hours || 0) > 0
                ? <span className="pill shortage">Перегруз</span>
                : <span className="pill ready">ОК</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
