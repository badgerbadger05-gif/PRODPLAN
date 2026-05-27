import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
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
  getShortageReport,
} from '../../services/planning'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

type Tab = 'production' | 'purchases' | 'rework' | 'capacity'

const limit = 200

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
  const [summary, setSummary] = useState<MrpSummary | null>(null)
  const [tab, setTab] = useState<Tab>('production')
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
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [selectedProductionIds, setSelectedProductionIds] = useState<Set<number>>(new Set())
  const [selectedPurchaseIds, setSelectedPurchaseIds] = useState<Set<number>>(new Set())
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const activeTotal = tab === 'production' ? productionTotal : tab === 'purchases' ? purchaseTotal : tab === 'rework' ? reworkTotal : capacityTotal
  const activeRowsLength = tab === 'production' ? productionRows.length : tab === 'purchases' ? purchaseRows.length : tab === 'rework' ? reworkRows.length : capacityRows.length
  const selectedCount = tab === 'production' ? selectedProductionIds.size : tab === 'purchases' ? selectedPurchaseIds.size : 0

  const totals = useMemo(() => ({
    productionQty: productionRows.reduce((sum, row) => sum + Number(row.qty || 0), 0),
    purchaseQty: purchaseRows.reduce((sum, row) => sum + Number(row.qty || 0), 0),
    reworkQty: reworkRows.reduce((sum, row) => sum + Number(row.qty || 0), 0),
    overloadHours: capacityRows.reduce((sum, row) => sum + Number(row.overload_hours || 0), 0),
  }), [productionRows, purchaseRows, reworkRows, capacityRows])

  const load = useCallback(async (nextDateFrom = '', nextDateTo = '') => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const params = { date_from: nextDateFrom || undefined, date_to: nextDateTo || undefined, limit, offset: 0 }
      const [summaryData, productionData, purchaseData, reworkData, capacityData] = await Promise.all([
        getPlanningRunSummary(runId),
        getPlanningResultProduction(runId, params),
        getPlanningResultPurchases(runId, params),
        getPlanningResultRework(runId, params),
        getPlanningResultCapacity(runId, params),
      ])
      setSummary(summaryData)
      setProductionRows(productionData.rows ?? [])
      setProductionTotal(productionData.total ?? 0)
      setPurchaseRows(purchaseData.rows ?? [])
      setPurchaseTotal(purchaseData.total ?? 0)
      setReworkRows(reworkData.rows ?? [])
      setReworkTotal(reworkData.total ?? 0)
      setCapacityRows(capacityData.rows ?? [])
      setCapacityTotal(capacityData.total ?? 0)
      setSelectedProductionIds(new Set())
      setSelectedPurchaseIds(new Set())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [runId])

  async function exportActive(format: 'csv' | 'xlsx') {
    setExporting(true)
    setError('')
    try {
      const params = { format, date_from: dateFrom || undefined, date_to: dateTo || undefined }
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
      await load(dateFrom, dateTo)
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

  async function exportShortageReport() {
    setExporting(true)
    setError('')
    try {
      const response = await getShortageReport(runId)
      downloadBase64File(response, `mrp_shortage_${runId}.xlsx`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  useEffect(() => {
    void load()
  }, [load])

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
            visibleFrom={activeTotal ? 1 : 0}
            visibleTo={Math.min(activeRowsLength, activeTotal)}
            total={activeTotal}
            selectedCount={selectedCount}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => navigate('/mrp-runs')}>К списку прогонов</button>
          <button onClick={() => void load(dateFrom, dateTo)} disabled={loading}>Обновить</button>
          {tab !== 'capacity' && <button onClick={() => void exportActive('csv')} disabled={loading || exporting}>CSV</button>}
          {tab !== 'capacity' && <button onClick={() => void exportActive('xlsx')} disabled={loading || exporting}>XLSX</button>}
          <button onClick={() => void exportShortageReport()} disabled={loading || exporting}>Отчёт дефицитов</button>
          {tab === 'production' && <button className="primary" onClick={() => void createSelectedProductionOrders()} disabled={!selectedProductionIds.size || loading || exporting}>Создать заказы ({selectedProductionIds.size})</button>}
          {tab === 'purchases' && <button className="primary" onClick={() => void exportSelectedPurchasesTo1C()} disabled={!selectedPurchaseIds.size || loading || exporting}>Выгрузить в 1С ({selectedPurchaseIds.size})</button>}
          <div className="barSeparator" />
          <label className="inlineControl">
            <span>С</span>
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
          </label>
          <label className="inlineControl">
            <span>По</span>
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
          </label>
          <button className="filterBtn" onClick={() => void load(dateFrom, dateTo)} disabled={loading}>Сформировать</button>
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
          {tab === 'production' && <ProductionResultTable rows={productionRows} selectedIds={selectedProductionIds} onSelectedIdsChange={setSelectedProductionIds} />}
          {tab === 'purchases' && <PurchaseResultTable rows={purchaseRows} selectedIds={selectedPurchaseIds} onSelectedIdsChange={setSelectedPurchaseIds} />}
          {tab === 'rework' && <ReworkResultTable rows={reworkRows} />}
          {tab === 'capacity' && <CapacityResultTable rows={capacityRows} />}
        </div>
      </DocumentWindow>
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

function ProductionResultTable({ rows, selectedIds, onSelectedIdsChange }: {
  rows: MrpProductionRow[]
  selectedIds: Set<number>
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
          <tr key={row.order_id}>
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

function PurchaseResultTable({ rows, selectedIds, onSelectedIdsChange }: {
  rows: MrpPurchaseRow[]
  selectedIds: Set<number>
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
          <th>К заказу</th>
          <th>Потребность</th>
          <th>Заказать до</th>
          <th>Срок пост.</th>
          <th>Покрыто</th>
          <th>Примечание</th>
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
          <tr key={row.purchase_id}>
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

function ReworkResultTable({ rows }: { rows: MrpReworkRow[] }) {
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
          <tr key={row.rework_id}>
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
