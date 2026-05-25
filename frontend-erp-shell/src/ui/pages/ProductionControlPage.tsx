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
            />
          )}
        </div>
      </DocumentWindow>
    </main>
  )
}
