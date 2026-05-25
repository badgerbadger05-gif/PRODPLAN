import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  coverageLabels,
  type ControlSettings,
  type ControlWarehouse,
  type MaterialIssueCreateResponse,
  type MaterialsResponse,
  type OrderRow,
  type OrdersResponse,
  type ProductionFilters,
  type WorkshopWarehouse,
} from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'
import { api } from '../../lib/api'
import { listResources } from '../../services/resources'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { ProductionCommandBar } from './production-control/ProductionCommandBar'
import { ProductionDetailPane } from './production-control/ProductionDetailPane'
import { ProductionFilterBar } from './production-control/ProductionFilterBar'
import { ProductionOrdersTable } from './production-control/ProductionOrdersTable'
import { ProductionSettingsPane } from './production-control/ProductionSettingsPane'

const limit = 100

export function ProductionControlPage() {
  const [rows, setRows] = useState<OrderRow[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [activeId, setActiveId] = useState<number | null>(null)
  const [materials, setMaterials] = useState<MaterialsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [total, setTotal] = useState(0)
  const [runId, setRunId] = useState<number | null>(null)
  const [offset, setOffset] = useState(0)
  const [filters, setFilters] = useState<ProductionFilters>({ search: '', status: '', workshop_id: '', date_from: '', date_to: '' })
  const filtersRef = useRef(filters)
  const offsetRef = useRef(offset)
  const [message, setMessage] = useState('')
  const [resources, setResources] = useState<ProductionResource[]>([])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [warehouses, setWarehouses] = useState<ControlWarehouse[]>([])
  const [workshopRows, setWorkshopRows] = useState<WorkshopWarehouse[]>([])
  const [ignoredRefs, setIgnoredRefs] = useState<Set<string>>(new Set())
  const [produceOpen, setProduceOpen] = useState(false)
  const [produceQty, setProduceQty] = useState('')
  const [producePerformer, setProducePerformer] = useState('')
  const [produceDryRun, setProduceDryRun] = useState(false)
  const [produceSaving, setProduceSaving] = useState(false)
  const [produceDryRunPayload, setProduceDryRunPayload] = useState<string | null>(null)

  useEffect(() => {
    filtersRef.current = filters
  }, [filters])

  useEffect(() => {
    offsetRef.current = offset
  }, [offset])

  const activeRow = useMemo(() => rows.find((row) => row.product_id === activeId) ?? rows[0] ?? null, [rows, activeId])
  const selectedRows = useMemo(() => rows.filter((row) => selectedIds.has(row.product_id)), [rows, selectedIds])

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const params = new URLSearchParams()
      params.set('limit', String(limit))
      params.set('offset', String(nextOffset))
      Object.entries(filtersRef.current).forEach(([key, value]) => {
        if (value) params.set(key, value)
      })
      const data = await api<OrdersResponse>(`/v1/production-control/orders?${params.toString()}`)
      setRows(data.rows ?? [])
      setTotal(data.total ?? 0)
      setRunId(data.latest_run_id ?? null)
      setOffset(nextOffset)
      setActiveId((current) => {
        if (current && data.rows?.some((row) => row.product_id === current)) return current
        return data.rows?.[0]?.product_id ?? null
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadResources = useCallback(async () => {
    try {
      setResources(await listResources())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  async function openSettings() {
    setSettingsOpen(true)
    setLoading(true)
    setError('')
    try {
      const [settingsData, resourcesData] = await Promise.all([
        api<ControlSettings>('/v1/production-control/settings'),
        resources.length ? Promise.resolve(resources) : listResources(),
      ])
      setResources(resourcesData)
      setWarehouses(settingsData.warehouses ?? [])
      setWorkshopRows(settingsData.workshop_warehouses ?? [])
      setIgnoredRefs(new Set((settingsData.ignored_warehouses ?? []).map((row) => row.warehouse_ref1c).filter(Boolean)))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function saveSettings() {
    setSettingsSaving(true)
    setError('')
    try {
      const saved = await api<ControlSettings>('/v1/production-control/settings', {
        method: 'POST',
        body: JSON.stringify({
          workshop_warehouses: workshopRows.filter((row) => row.warehouse_ref1c),
          ignored_warehouses: Array.from(ignoredRefs).map((warehouse_ref1c) => ({ warehouse_ref1c })),
        }),
      })
      setWarehouses(saved.warehouses ?? [])
      setWorkshopRows(saved.workshop_warehouses ?? [])
      setIgnoredRefs(new Set((saved.ignored_warehouses ?? []).map((row) => row.warehouse_ref1c).filter(Boolean)))
      setSettingsOpen(false)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSettingsSaving(false)
    }
  }

  const loadMaterials = useCallback(async (row: OrderRow) => {
    setActiveId(row.product_id)
    setMaterials(null)
    try {
      setMaterials(await api<MaterialsResponse>(`/v1/production-control/orders/${row.product_id}/materials`))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  async function changeStatus(row: OrderRow, status: string) {
    const previous = row.status
    setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status } : item))
    try {
      await api(`/v1/production-control/orders/${row.product_id}/state`, {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      })
    } catch (e) {
      setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status: previous } : item))
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function startSelected() {
    if (!selectedIds.size) return
    setLoading(true)
    try {
      await api('/v1/production-control/orders/start-in-1c', {
        method: 'POST',
        body: JSON.stringify({ product_ids: Array.from(selectedIds) }),
      })
      setSelectedIds(new Set())
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function createMaterialIssues() {
    if (!selectedIds.size) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
        method: 'POST',
        body: JSON.stringify({ product_ids: Array.from(selectedIds), initiated_by: 'erp-shell' }),
      })
      const created = result.created?.length ?? 0
      const errors = result.errors?.length ?? 0
      setSelectedIds(new Set())
      await load(offsetRef.current)
      setMessage(`Выдача материалов: создано документов ${created}${errors ? `, ошибок ${errors}` : ''}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function exportTo1C() {
    if (!selectedIds.size) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api<Record<string, unknown>>('/v1/production-control/orders/export-to-1c', {
        method: 'POST',
        body: JSON.stringify({ product_ids: Array.from(selectedIds) }),
      })
      const created = Number(result.created ?? 0)
      const skipped = Number(result.skipped ?? 0)
      const errors = Number(result.errors ?? 0)
      setMessage(`Экспорт в 1С: создано ${created}${skipped ? `, пропущено ${skipped}` : ''}${errors ? `, ошибок ${errors}` : ''}`)
      setSelectedIds(new Set())
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function syncFrom1C() {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const body = selectedIds.size ? { product_ids: Array.from(selectedIds) } : {}
      const result = await api<Record<string, unknown>>('/v1/production-control/orders/sync-from-1c', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const checked = Number(result.checked ?? 0)
      const updated = Number(result.updated_to_assembled ?? 0)
      const errors = Array.isArray(result.errors) ? result.errors.length : 0
      setMessage(`Синхронизация: проверено ${checked}, обновлено ${updated}${errors ? `, ошибок ${errors}` : ''}`)
      if (updated > 0) {
        setSelectedIds(new Set())
        await load(offsetRef.current)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function openProduceDialog() {
    if (!activeRow) return
    setProduceQty(String(activeRow.remaining_qty ?? activeRow.quantity ?? 0))
    setProducePerformer('')
    setProduceDryRun(false)
    setProduceDryRunPayload(null)
    setProduceOpen(true)
  }

  async function submitProduce() {
    if (!activeRow) return
    setProduceSaving(true)
    setError('')
    try {
      const result = await api<Record<string, unknown>>(
        `/v1/production-control/orders/${activeRow.product_id}/produce-to-1c`,
        {
          method: 'POST',
          body: JSON.stringify({
            qty: Number(produceQty) || 0,
            performer: producePerformer || '',
            dry_run: produceDryRun,
          }),
        },
      )
      if (result.status === 'dry_run') {
        setProduceDryRunPayload(
          JSON.stringify({ assembly: result.assembly_payload, piecework: result.piecework_payload }, null, 2),
        )
      } else if (result.status === 'created') {
        setMessage(
          `Произведено: ${result.new_produced_qty}, осталось: ${result.new_remaining_qty}. СборкаЗапасов: ${String(result.assembly_ref1c ?? '').slice(0, 8)}...`,
        )
        setProduceOpen(false)
        setSelectedIds(new Set())
        await load(offsetRef.current)
      } else {
        setError(String(result.error ?? 'Неизвестная ошибка'))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setProduceSaving(false)
    }
  }

  async function fillRemaining(runId: number, requirementId: number) {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api<Record<string, unknown>>('/v1/production-control/orders/from-mrp', {
        method: 'POST',
        body: JSON.stringify({ run_id: runId, planned_order_ids: [requirementId] }),
      })
      const created = Number(result.created_count ?? (Array.isArray(result.created) ? result.created.length : 0))
      const existing = Number(result.existing_count ?? (Array.isArray(result.existing) ? result.existing.length : 0))
      setMessage(`Досоздано: новых ${created}, уже было ${existing}`)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function saveOptimalBatch(itemId: number, value: number | null) {
    const item = await api<Record<string, unknown>>(`/v1/items/${itemId}`)
    await api(`/v1/items/${itemId}`, {
      method: 'PUT',
      body: JSON.stringify({
        item_code: String(item.item_code ?? ''),
        item_name: String(item.item_name ?? ''),
        item_article: item.item_article ?? null,
        item_ref1c: item.item_ref1c ?? null,
        supplier_ref1c: item.supplier_ref1c ?? null,
        replenishment_time: item.replenishment_time ?? null,
        unit: item.unit ?? null,
        category_id: item.category_id ?? null,
        stock_qty: Number(item.stock_qty ?? 0),
        optimal_batch: value,
        status: String(item.status ?? 'active'),
      }),
    })
    setRows((list) => list.map((row) => row.item_id === itemId ? { ...row, optimal_batch: value } : row))
  }

  async function saveOrderQuantity(productId: number, value: number) {
    const result = await api<{ quantity: number; remaining_qty: number }>(`/v1/production-control/orders/${productId}/quantity`, {
      method: 'PATCH',
      body: JSON.stringify({ quantity: value }),
    })
    setRows((list) => list.map((row) => row.product_id === productId ? {
      ...row,
      quantity: Number(result.quantity ?? value),
      remaining_qty: Number(result.remaining_qty ?? value),
    } : row))
  }

  function printRows(ids: number[]) {
    if (!ids.length) return
    window.open(`/api/v1/production-control/route-sheets/print?product_ids=${ids.join(',')}`, '_blank')
  }

  useEffect(() => {
    void load(0)
    void loadResources()
  }, [load, loadResources])

  useEffect(() => {
    if (activeRow) void loadMaterials(activeRow)
  }, [activeRow, loadMaterials])

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Журнал заказов на производство</div>
        <div className="runBadge">MRP run: {runId ?? '—'}</div>
      </div>

      <DocumentWindow
        title="Журнал заказов на производство"
        subtitle="Рабочий список строк по деталям, цехам, обеспечению и запуску в 1С"
        hotkeys="F5 Обновить · Ctrl+P Печать · Enter Детали"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={selectedRows.length}
            canPrev={offset > 0}
            canNext={offset + rows.length < total}
            onPrev={() => void load(Math.max(0, offset - limit))}
            onNext={() => void load(offset + limit)}
          />
        )}
      >
        <ProductionCommandBar
          activeRow={activeRow}
          rows={rows}
          selectedIds={selectedIds}
          loading={loading}
          onStartSelected={() => void startSelected()}
          onExportTo1C={() => void exportTo1C()}
          onSyncFrom1C={() => void syncFrom1C()}
          onProduce={() => openProduceDialog()}
          onPrintSelected={() => printRows(Array.from(selectedIds))}
          onCreateMaterialIssues={() => void createMaterialIssues()}
          onLoadMaterials={() => activeRow && void loadMaterials(activeRow)}
          onOpenSettings={() => void openSettings()}
          onRefresh={() => void load(offset)}
          onSelectAll={() => setSelectedIds(new Set(rows.map((row) => row.product_id)))}
          onClearSelection={() => setSelectedIds(new Set())}
        />

        <ProductionFilterBar filters={filters} resources={resources} onChange={setFilters} onSubmit={() => void load(0)} />

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split">
          <div className="tablePane">
            <ProductionOrdersTable
              rows={rows}
              activeRow={activeRow}
              selectedIds={selectedIds}
              onSelectIds={setSelectedIds}
              onActivate={setActiveId}
              onOpenMaterials={(row) => void loadMaterials(row)}
              onChangeStatus={(row, status) => void changeStatus(row, status)}
            />
          </div>

          {settingsOpen ? (
            <ProductionSettingsPane
              resources={resources}
              warehouses={warehouses}
              workshopRows={workshopRows}
              ignoredRefs={ignoredRefs}
              saving={settingsSaving}
              onWorkshopRowsChange={setWorkshopRows}
              onIgnoredRefsChange={setIgnoredRefs}
              onSave={() => void saveSettings()}
              onClose={() => setSettingsOpen(false)}
            />
          ) : (
            <ProductionDetailPane
              activeRow={activeRow}
              materials={materials}
              coverageLabels={coverageLabels}
              onLoadMaterials={() => activeRow && void loadMaterials(activeRow)}
              onPrint={() => activeRow && printRows([activeRow.product_id])}
              onOptimalBatchSave={(itemId, value) => saveOptimalBatch(itemId, value)}
              onQuantitySave={(productId, value) => saveOrderQuantity(productId, value)}
              onFillRemaining={(sourceRunId, requirementId) => fillRemaining(sourceRunId, requirementId)}
            />
          )}
        </div>
      </DocumentWindow>

      {produceOpen && activeRow && (
        <div className="dialogOverlay" onClick={(e) => { if (e.target === e.currentTarget) setProduceOpen(false) }}>
          <div className="dialogBox">
            <div className="dialogHeader">Произвести - {activeRow.item_name}</div>
            <div className="dialogBody">
              <div className="dialogField">
                <label>Количество ({activeRow.unit || 'шт'})</label>
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={produceQty}
                  onChange={(e) => setProduceQty(e.target.value)}
                  disabled={produceSaving}
                />
              </div>
              <div className="dialogField">
                <label>Исполнитель (имя, войдёт в комментарий)</label>
                <input
                  type="text"
                  value={producePerformer}
                  onChange={(e) => setProducePerformer(e.target.value)}
                  placeholder="Иванов И.И."
                  disabled={produceSaving}
                />
              </div>
              <div className="dialogCheckRow">
                <input
                  type="checkbox"
                  id="produceDryRun"
                  checked={produceDryRun}
                  onChange={(e) => { setProduceDryRun(e.target.checked); setProduceDryRunPayload(null) }}
                  disabled={produceSaving}
                />
                <label htmlFor="produceDryRun">dry_run - показать payload, не отправлять в 1С</label>
              </div>
              {produceDryRunPayload && <div className="dialogPreview">{produceDryRunPayload}</div>}
            </div>
            <div className="dialogFooter">
              <button onClick={() => setProduceOpen(false)} disabled={produceSaving}>Отмена</button>
              <button className="primary" onClick={() => void submitProduce()} disabled={produceSaving}>
                {produceSaving ? 'Создаём...' : produceDryRun ? 'Показать payload' : 'Создать в 1С'}
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
