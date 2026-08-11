import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import type { MrpCapacityRow, MrpProductionRow, MrpPurchaseRow, MrpReworkRow, MrpSummary } from '../../domain/planning'
import { planningStatusLabel } from '../../domain/planning'
import { downloadBase64File } from '../../lib/download'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import {
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
import { RootProductFilterDialog } from '../RootProductFilterDialog'
import { rootProductLabel, type RootProductOption } from '../rootProductOptions'
import { StatusBar } from '../layout/StatusBar'
import { ForecastShift } from './period-plan/ForecastShift'
import {
  buildPurchaseCategoryFilterParam,
  formatActionResult,
  parseMrpResultTab,
  parsePositiveId,
  productionSourceIds,
  purchaseFilterOptions,
  purchaseSourceIds,
  supplierDisplayName,
  toggleMany,
  type MrpResultTab,
} from './mrp-result/model'

type Tab = MrpResultTab

const limit = 200

function emptyTabFlags(): Record<Tab, boolean> {
  return { production: false, purchases: false, rework: false, capacity: false }
}

function emptyTabOffsets(): Record<Tab, number> {
  return { production: 0, purchases: 0, rework: 0, capacity: 0 }
}

function metricQuantity(value: number | null | undefined, unit: string) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${qty(value)} ${unit}`
    : 'н/д'
}

function buildPurchaseApiFilters(supplierFilter: string, categoryFilter: string) {
  return {
    supplier_ref1c: supplierFilter || undefined,
    ...buildPurchaseCategoryFilterParam(categoryFilter),
  }
}

export function MrpResultPage() {
  const { runId: runIdParam } = useParams<{ runId: string }>()
  const runId = Number(runIdParam)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryTab = parseMrpResultTab(searchParams.get('tab'))
  const highlightedProductionId = parsePositiveId(searchParams.get('planned_order_id'))
  const highlightedPurchaseId = parsePositiveId(searchParams.get('purchase_id'))
  const highlightedReworkId = parsePositiveId(searchParams.get('rework_id'))
  const [summary, setSummary] = useState<MrpSummary | null>(null)
  const [dataRunId, setDataRunId] = useState(runId)
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
  const [selectedPurchaseIds, setSelectedPurchaseIds] = useState<Set<number>>(new Set())
  const [purchaseSupplierFilter, setPurchaseSupplierFilter] = useState('')
  const [purchaseCategoryFilter, setPurchaseCategoryFilter] = useState('')
  const [rootItemId, setRootItemId] = useState<number | null>(null)
  const [rootOptions, setRootOptions] = useState<RootProductOption[]>([])
  const [rootDialogOpen, setRootDialogOpen] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const loadSeq = useRef(0)
  const summarySeq = useRef(0)
  const mutationInFlight = useRef(false)
  const previousRunId = useRef(runId)
  const snapshotId = summary?.snapshot_id ?? null
  const truthAccepted = summary?.truth_status === 'accepted' && snapshotId !== null
  const truthUnavailableReason = summary && !truthAccepted
    ? summary.truth_reason || `Снимок MRP недоступен: ${summary.truth_status || 'unavailable'}`
    : ''

  const purchaseOptions = useMemo(() => purchaseFilterOptions(purchaseRows), [purchaseRows])
  const purchaseSupplierOptions = purchaseOptions.suppliers
  const purchaseCategoryOptions = purchaseOptions.categories
  const activeOffset = offsets[tab]
  const activeTotal = tab === 'production' ? productionTotal : tab === 'purchases' ? purchaseTotal : tab === 'rework' ? reworkTotal : capacityTotal
  const activeRowsLength = tab === 'production' ? productionRows.length : tab === 'purchases' ? purchaseRows.length : tab === 'rework' ? reworkRows.length : capacityRows.length
  const activeVisibleFrom = activeTotal && activeRowsLength ? activeOffset + 1 : 0
  const activeVisibleTo = activeTotal && activeRowsLength ? Math.min(activeOffset + activeRowsLength, activeTotal) : 0
  const selectedCount = tab === 'purchases' ? selectedPurchaseIds.size : 0

  useEffect(() => {
    if (queryTab) setTab(queryTab)
  }, [queryTab])

  const invalidateTabs = useCallback(() => {
    loadSeq.current += 1
    setLoading(false)
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
    setSelectedPurchaseIds(new Set())
    setPurchaseSupplierFilter('')
    setPurchaseCategoryFilter('')
  }, [])

  const loadSummary = useCallback(async () => {
    const seq = ++summarySeq.current
    invalidateTabs()
    try {
      const nextSummary = await getPlanningRunSummary(runId)
      if (seq === summarySeq.current) {
        setSummary(nextSummary)
        if (nextSummary.truth_status !== 'accepted' || nextSummary.snapshot_id === null) {
          setError(nextSummary.truth_reason || `Снимок MRP недоступен: ${nextSummary.truth_status || 'unavailable'}`)
        } else {
          setError('')
        }
      }
    } catch (e) {
      if (seq === summarySeq.current) {
        setSummary(null)
        setError(e instanceof Error ? e.message : String(e))
      }
    }
  }, [invalidateTabs, runId])

  const loadTab = useCallback(async (targetTab: Tab, nextOffset: number) => {
    const seq = ++loadSeq.current
    setLoading(true)
    setError('')
    if (!truthAccepted || snapshotId === null) {
      invalidateTabs()
      return
    }
    try {
      const params = {
        snapshot_id: snapshotId,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        root_item_id: rootItemId,
        limit,
        offset: nextOffset,
      }
      let data
      if (targetTab === 'production') {
        data = await getPlanningResultProduction(runId, params)
        if (seq !== loadSeq.current) return
        setProductionRows(data.rows ?? [])
        setProductionTotal(data.total ?? 0)
      } else if (targetTab === 'purchases') {
        data = await getPlanningResultPurchases(runId, {
          ...params,
          ...buildPurchaseApiFilters(purchaseSupplierFilter, purchaseCategoryFilter),
        })
        if (seq !== loadSeq.current) return
        setPurchaseRows(data.rows ?? [])
        setPurchaseTotal(data.total ?? 0)
      } else if (targetTab === 'rework') {
        data = await getPlanningResultRework(runId, params)
        if (seq !== loadSeq.current) return
        setReworkRows(data.rows ?? [])
        setReworkTotal(data.total ?? 0)
      } else {
        data = await getPlanningResultCapacity(runId, params)
        if (seq !== loadSeq.current) return
        setCapacityRows(data.rows ?? [])
        setCapacityTotal(data.total ?? 0)
      }
      const identityMatches = data.truth_status === 'accepted'
        && data.snapshot_id === snapshotId
        && data.ledger_generation === summary?.ledger_generation
        && data.cutoff === summary?.cutoff
      if (!identityMatches) {
        const mismatchReason = data.truth_reason || 'Ответ вкладки не соответствует зафиксированному снимку MRP'
        setSummary((current) => current ? {
          ...current,
          snapshot_id: null,
          truth_status: 'unavailable',
          truth_reason: mismatchReason,
        } : current)
        invalidateTabs()
        setError(mismatchReason)
        return
      }
      setOffsets((prev) => ({ ...prev, [targetTab]: nextOffset }))
      setLoadedTabs((prev) => ({ ...prev, [targetTab]: true }))
    } catch (e) {
      if (seq === loadSeq.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [dateFrom, dateTo, invalidateTabs, purchaseCategoryFilter, purchaseSupplierFilter, rootItemId, runId, snapshotId, summary, truthAccepted])

  useEffect(() => {
    if (previousRunId.current === runId) return
    previousRunId.current = runId
    summarySeq.current += 1
    setSummary(null)
    invalidateTabs()
    setDataRunId(runId)
  }, [invalidateTabs, runId])

  useEffect(() => {
    void loadSummary()
  }, [loadSummary])

  useEffect(() => {
    if (dataRunId === runId && truthAccepted && !loadedTabs[tab]) void loadTab(tab, offsets[tab])
  }, [dataRunId, loadedTabs, loadTab, offsets, runId, tab, truthAccepted])

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
    if (!truthAccepted || snapshotId === null) return
    setExporting(true)
    setError('')
    try {
      const baseParams = { snapshot_id: snapshotId, format, date_from: dateFrom || undefined, date_to: dateTo || undefined, root_item_id: rootItemId }
      const params = tab === 'purchases'
        ? { ...baseParams, ...buildPurchaseApiFilters(purchaseSupplierFilter, purchaseCategoryFilter) }
        : baseParams
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

  async function exportSelectedPurchasesTo1C() {
    if (!truthAccepted || !selectedPurchaseIds.size || mutationInFlight.current) return
    mutationInFlight.current = true
    setExporting(true)
    setError('')
    setMessage('')
    try {
      const result = await exportPurchasesTo1C(runId, {
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        purchase_ids: Array.from(selectedPurchaseIds),
        dry_run: false,
        allow_production: true,
      })
      setMessage(formatActionResult('Выгрузка закупок в 1С', result))
      setSelectedPurchaseIds(new Set())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      mutationInFlight.current = false
      setExporting(false)
    }
  }

  function applyDateFilters() {
    setDateFrom(draftDateFrom)
    setDateTo(draftDateTo)
    invalidateTabs()
  }

  function applyPurchaseFilters(nextSupplierFilter: string, nextCategoryFilter: string) {
    setPurchaseSupplierFilter(nextSupplierFilter)
    setPurchaseCategoryFilter(nextCategoryFilter)
    setSelectedPurchaseIds(new Set())
    setPurchaseRows([])
    setPurchaseTotal(0)
    setLoadedTabs((prev) => ({ ...prev, purchases: false }))
    setOffsets((prev) => ({ ...prev, purchases: 0 }))
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
        <div className="runBadge">{truthUnavailableReason ? 'Данные недоступны' : summary?.run?.status ? planningStatusLabel(summary.run.status) : 'Загрузка'}</div>
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
          <button onClick={() => { void loadSummary() }} disabled={loading}>Обновить</button>
          {tab !== 'capacity' && <button onClick={() => void exportActive('xlsx')} disabled={!truthAccepted || loading || exporting}>XLSX</button>}
          {tab === 'purchases' && <button className="primary" onClick={() => void exportSelectedPurchasesTo1C()} disabled={!truthAccepted || !selectedPurchaseIds.size || loading || exporting}>Выгрузить в 1С ({selectedPurchaseIds.size})</button>}
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
        {truthUnavailableReason && !error && <div className="errorLine">{truthUnavailableReason}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="mrpSummaryStrip">
          <Metric title="Старт" value={truthAccepted ? dateTimeRu(summary?.run?.started_at) || '—' : 'Недоступно'} />
          <Metric title="Горизонт" value={truthAccepted ? `${qty(summary?.run?.horizon_days)} дн.` : 'Недоступно'} />
          <Metric title="Производство" value={truthAccepted ? qty(summary?.counts?.production_orders ?? productionTotal) : 'Недоступно'} hint={truthAccepted ? metricQuantity(summary?.snapshot_total_qty?.production, 'шт.') : undefined} />
          <Metric title="Закупки" value={truthAccepted ? qty(summary?.counts?.purchase_requests ?? purchaseTotal) : 'Недоступно'} hint={truthAccepted ? metricQuantity(summary?.snapshot_total_qty?.purchase, 'шт.') : undefined} />
          <Metric title="Переработка" value={truthAccepted ? qty(summary?.counts?.rework_requests ?? reworkTotal) : 'Недоступно'} hint={truthAccepted ? metricQuantity(summary?.snapshot_total_qty?.rework, 'шт.') : undefined} />
          <Metric title="Перегрузы" value={truthAccepted ? qty(summary?.capacity?.overloaded_buckets) : 'Недоступно'} hint={truthAccepted ? metricQuantity(summary?.capacity?.overload_total, 'н/ч') : undefined} />
        </div>

        <div className="tabsBar">
          <button className={tab === 'production' ? 'activeTab' : ''} onClick={() => setTab('production')}>Производство</button>
          <button className={tab === 'purchases' ? 'activeTab' : ''} onClick={() => setTab('purchases')}>Закупки</button>
          <button className={tab === 'rework' ? 'activeTab' : ''} onClick={() => setTab('rework')}>Переработка</button>
          <button className={tab === 'capacity' ? 'activeTab' : ''} onClick={() => setTab('capacity')}>Мощности</button>
        </div>

        <div className="tablePane resultTablePane">
          {!truthAccepted && <div className="emptyState">Фактические данные MRP недоступны. Дождитесь принятого Ledger-снимка.</div>}
          {tab === 'production' && <ProductionResultTable rows={productionRows} highlightedId={highlightedProductionId} />}
          {tab === 'purchases' && (
            <PurchaseResultTable
              rows={purchaseRows}
              selectedIds={selectedPurchaseIds}
              highlightedId={highlightedPurchaseId}
              supplierFilter={purchaseSupplierFilter}
              categoryFilter={purchaseCategoryFilter}
              supplierOptions={purchaseSupplierOptions}
              categoryOptions={purchaseCategoryOptions}
              onSupplierFilterChange={(value) => applyPurchaseFilters(value, purchaseCategoryFilter)}
              onCategoryFilterChange={(value) => applyPurchaseFilters(purchaseSupplierFilter, value)}
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

function ProductionResultTable({ rows, highlightedId }: {
  rows: MrpProductionRow[]
  highlightedId: number | null
}) {
  return (
    <table className="journalTable resultTable">
      <thead>
        <tr>
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
          <tr key={row.order_id} className={highlightedId && productionSourceIds(row).includes(highlightedId) ? 'activeRow' : undefined}>
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
  const visibleIds = rows.flatMap(purchaseSourceIds)
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
          const coverageLabel = row.supplier_coverage_label || '—'
          const coverageStatus = row.supplier_coverage_status
          return (
          <tr key={row.purchase_id} className={highlightedId && purchaseSourceIds(row).includes(highlightedId) ? 'activeRow' : undefined}>
            <td className="checkCol">
              <input
                type="checkbox"
                checked={purchaseSourceIds(row).every((id) => selectedIds.has(id))}
                onChange={(e) => onSelectedIdsChange(toggleMany(selectedIds, purchaseSourceIds(row), e.target.checked))}
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
            <td className="numCell" title={`Покрыто активными заказами поставщику: ${coverageLabel}`} style={{ color: coverageStatus === 'full' ? 'var(--color-success, green)' : coverageStatus === 'partial' ? 'var(--color-warning, orange)' : undefined }}>
              {coverageLabel}
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
              {row.capacity_status === 'overloaded'
                ? <span className="pill shortage">Перегруз</span>
                : row.capacity_status === 'within_capacity'
                  ? <span className="pill ready">ОК</span>
                  : <span className="pill unavailable">Недоступно</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
