import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  coverageLabels,
  type ControlWarehouse,
  type MaterialIssueCreateResponse,
  type MaterialsResponse,
  type OrderRow,
  type ProductionFilters,
  type WarehouseCandidate,
  type WorkshopWarehouse,
} from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'
import { getPeriodPlanMatrix, listPeriodPlans } from '../../services/periodPlan'
import {
  deleteProductionOrder,
  exportMaterialIssuesTo1C,
  fetchRouteSheetsPrintHtml,
  getOrderMaterials,
  getProductionControlSettings,
  listProductionOrders,
  postMaterialIssues,
  produceOrderLine,
  refreshOrderMaterials,
  returnLeftoverComponents,
  saveProductionControlSettings,
  syncPostedTransfers,
  updateItem,
  updateOrderQuantity,
  updateOrderStatus,
} from '../../services/productionControl'
import { listResources } from '../../services/resources'
import { DocumentWindow } from '../layout/DocumentWindow'
import { RootProductFilterDialog } from '../RootProductFilterDialog'
import { rootProductLabel, type RootProductOption } from '../rootProductOptions'
import { StatusBar } from '../layout/StatusBar'
import { AsyncState } from '../layout/AsyncState'
import { ProductionCommandBar } from './production-control/ProductionCommandBar'
import { ProductionDetailPane } from './production-control/ProductionDetailPane'
import { ProductionFilterBar } from './production-control/ProductionFilterBar'
import { ProductionOrdersTable } from './production-control/ProductionOrdersTable'
import { ProductionSettingsPane } from './production-control/ProductionSettingsPane'
import { ProductionViewBar } from './production-control/ProductionViewBar'
import type { ProductionOrderSortKey } from './production-control/productionOrdersDoctype'
import { WarehousePickerDialog } from './production-control/WarehousePickerDialog'
import { firstExportProblem, issueIdsFromCreateResult, limit } from './production-control/helpers'
import {
  activeProductionRow,
  applyMaterialCoverage,
  buildProductionOrderParams,
  buildProductionSettingsPayload,
  nextProductionSort,
  productionPagination,
  selectedProductionRows,
} from './production-control/model'

export function ProductionControlPage() {
  const listRequestSeq = useRef(0)
  const materialsRequestSeq = useRef(0)
  const dangerousMutationLocked = useRef(false)
  const [searchParams] = useSearchParams()
  const focusProductId = searchParams.get('product_id')
  const focusOrderId = searchParams.get('order_id')
  const [rows, setRows] = useState<OrderRow[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [activeId, setActiveId] = useState<number | null>(null)
  const [materials, setMaterials] = useState<MaterialsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [total, setTotal] = useState(0)
  const [runId, setRunId] = useState<number | null>(null)
  const [rootOptions, setRootOptions] = useState<RootProductOption[]>([])
  const [rootDialogOpen, setRootDialogOpen] = useState(false)
  const [offset, setOffset] = useState(0)
  const [filters, setFilters] = useState<ProductionFilters>({
    search: '',
    status: '',
    workshop_id: '',
    coverage_status: '',
    root_item_id: '',
    planning_contour: '',
    sort_by: 'planned_start_date',
    sort_dir: 'asc',
  })
  const filtersRef = useRef(filters)
  const offsetRef = useRef(offset)
  const [message, setMessage] = useState('')
  const [resources, setResources] = useState<ProductionResource[]>([])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [warehouses, setWarehouses] = useState<ControlWarehouse[]>([])
  const [workshopRows, setWorkshopRows] = useState<WorkshopWarehouse[]>([])
  const [ignoredRefs, setIgnoredRefs] = useState<Set<string>>(new Set())
  const [warehousePickerOpen, setWarehousePickerOpen] = useState(false)
  const [warehousePickerCandidates, setWarehousePickerCandidates] = useState<WarehouseCandidate[]>([])
  const [warehousePickerComponents, setWarehousePickerComponents] = useState<Array<{ item_name: string; item_article?: string | null; required_qty: number }>>([])
  const [warehousePickerProductIds, setWarehousePickerProductIds] = useState<number[]>([])
  const [warehousePickerSelected, setWarehousePickerSelected] = useState('')
  const [warehousePickerMode, setWarehousePickerMode] = useState<'issues' | 'export'>('issues')

  useEffect(() => {
    filtersRef.current = filters
  }, [filters])

  useEffect(() => {
    offsetRef.current = offset
  }, [offset])

  const activeRow = useMemo(() => activeProductionRow(rows, activeId), [rows, activeId])
  const selectedRows = useMemo(() => selectedProductionRows(rows, selectedIds), [rows, selectedIds])

  const load = useCallback(async (nextOffset: number) => {
    const requestSeq = ++listRequestSeq.current
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const params = buildProductionOrderParams({
        filters: filtersRef.current,
        offset: nextOffset,
        limit,
        focusProductId,
        focusOrderId,
      })
      const data = await listProductionOrders(params)
      if (requestSeq !== listRequestSeq.current) return
      setRows(data.rows ?? [])
      setTotal(data.total ?? 0)
      setRunId(data.latest_run_id ?? null)
      setOffset(data.offset ?? nextOffset)
      setActiveId((current) => {
        const focusedProductId = Number(focusProductId || 0)
        if (focusedProductId && data.rows?.some((row) => row.product_id === focusedProductId)) return focusedProductId
        if (current && data.rows?.some((row) => row.product_id === current)) return current
        return data.rows?.[0]?.product_id ?? null
      })
    } catch (e) {
      if (requestSeq !== listRequestSeq.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (requestSeq === listRequestSeq.current) setLoading(false)
    }
  }, [focusOrderId, focusProductId])

  const loadResources = useCallback(async () => {
    try {
      setResources(await listResources())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function loadRootOptions() {
      try {
        const plansData = await listPeriodPlans({
          status: 'fixed',
          limit: 500,
          sort_by: 'period_from',
          sort_dir: 'desc',
        })
        const matrices = await Promise.all(
          (plansData.rows ?? []).map((plan) => getPeriodPlanMatrix(plan.id).catch(() => null)),
        )
        if (cancelled) return
        const byItemId = new Map<number, RootProductOption>()
        matrices.forEach((data) => {
          ;(data?.rows ?? []).forEach((row) => {
            if (!byItemId.has(row.item_id)) {
              byItemId.set(row.item_id, {
                item_id: row.item_id,
                item_name: row.item_name,
                item_article: row.item_article,
                item_code: row.item_code,
              })
            }
          })
        })
        setRootOptions(Array.from(byItemId.values()).sort((a, b) => (
          (a.item_article || a.item_name || a.item_code || '').localeCompare(
            b.item_article || b.item_name || b.item_code || '',
            'ru',
          )
        )))
      } catch {
        if (!cancelled) setRootOptions([])
      }
    }
    void loadRootOptions()
    return () => { cancelled = true }
  }, [runId])

  async function openSettings() {
    setSettingsOpen(true)
    setLoading(true)
    setError('')
    try {
      const [settingsData, resourcesData] = await Promise.all([
        getProductionControlSettings(),
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
      const saved = await saveProductionControlSettings(buildProductionSettingsPayload(workshopRows, ignoredRefs))
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

  const requestMaterials = useCallback(async (
    productId: number,
    request: (id: number) => Promise<MaterialsResponse>,
    updateCoverage: boolean,
  ) => {
    const requestSeq = ++materialsRequestSeq.current
    setActiveId(productId)
    setMaterials(null)
    try {
      const data = await request(productId)
      if (requestSeq !== materialsRequestSeq.current) return
      setMaterials(data)
      const coverageStatus = String(data.coverage_status || '')
      if (updateCoverage && coverageStatus) {
        setRows((list) => applyMaterialCoverage(list, productId, coverageStatus, data.coverage_label))
      }
    } catch (e) {
      if (requestSeq !== materialsRequestSeq.current) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const loadMaterials = useCallback(
    (productId: number) => requestMaterials(productId, getOrderMaterials, false),
    [requestMaterials],
  )

  const refreshMaterials = useCallback(
    (productId: number) => requestMaterials(productId, refreshOrderMaterials, true),
    [requestMaterials],
  )

  function beginDangerousMutation() {
    if (dangerousMutationLocked.current) return false
    dangerousMutationLocked.current = true
    return true
  }

  function endDangerousMutation() {
    dangerousMutationLocked.current = false
  }

  async function changeStatus(row: OrderRow, status: string) {
    const previous = row.status
    setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status } : item))
    try {
      await updateOrderStatus(row.product_id, status)
    } catch (e) {
      setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status: previous } : item))
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function requestMaterialIssues(sourceWarehouseRef: string | undefined, productIds: number[]) {
    return postMaterialIssues(productIds, 'erp-shell', sourceWarehouseRef)
  }

  function showWarehousePicker(result: MaterialIssueCreateResponse, mode: 'issues' | 'export', productIds?: number[]) {
    const selectionRequired = result.selection_required ?? []
    if (!selectionRequired.length) return false
    const candidates = selectionRequired[0].warehouse_candidates
    const components = selectionRequired.flatMap((item) => item.components ?? [])
    setWarehousePickerCandidates(candidates)
    setWarehousePickerComponents(components.map((item) => ({
      item_name: item.item_name,
      item_article: item.item_article,
      required_qty: item.required_qty,
    })))
    setWarehousePickerProductIds(productIds?.length ? productIds : selectionRequired.map((item) => item.product_id))
    setWarehousePickerSelected(candidates[0]?.ref1c ?? '')
    setWarehousePickerMode(mode)
    setWarehousePickerOpen(true)
    return true
  }

  function prepareRouteSheetWindow() {
    const printWindow = window.open('', '_blank')
    if (!printWindow) {
      setError('Браузер заблокировал окно печати. Разрешите всплывающие окна для PRODPLAN.')
      return null
    }
    printWindow.document.write('<!doctype html><title>Маршрутные листы</title><body>Загрузка...</body>')
    return printWindow
  }

  function closeRouteSheetWindow(printWindow: Window | null) {
    if (printWindow && !printWindow.closed) printWindow.close()
  }

  function renderRouteSheets(ids: number[], printWindow: Window | null) {
    if (!ids.length || !printWindow || printWindow.closed) return
    void fetchRouteSheetsPrintHtml(ids)
      .then((html) => {
        printWindow.document.open()
        printWindow.document.write(html)
        printWindow.document.close()
        window.setTimeout(() => void load(offsetRef.current), 1200)
      })
      .catch((e) => {
        closeRouteSheetWindow(printWindow)
        setError(e instanceof Error ? e.message : String(e))
      })
  }

  function openRouteSheets(ids: number[]) {
    if (!ids.length) return
    renderRouteSheets(ids, prepareRouteSheetWindow())
  }

  async function createMaterialIssues(sourceWarehouseRef?: string, productIds?: number[]) {
    const ids = productIds ?? Array.from(selectedIds)
    if (!ids.length) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await requestMaterialIssues(sourceWarehouseRef, ids)
      const selectionRequired = result.selection_required ?? []
      const errors = result.errors?.length ?? 0
      const alreadyOnDestination = result.already_on_destination?.reduce((sum, row) => sum + (row.components?.length ?? 0), 0) ?? 0

      if (selectionRequired.length > 0) {
        showWarehousePicker(result, 'issues')
        const msg = `Создано документов: ${result.created?.length ?? 0}${alreadyOnDestination ? `, уже на участке ${alreadyOnDestination}` : ''}${errors ? `, ошибок ${errors}` : ''}. Для ${selectionRequired.length} поз. нужно выбрать склад-источник.`
        setMessage(msg)
      } else {
        setMessage(`Выдача материалов: создано документов ${result.created?.length ?? 0}${alreadyOnDestination ? `, уже на участке ${alreadyOnDestination}` : ''}${errors ? `, ошибок ${errors}` : ''}`)
      }
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setLoading(false)
    }
  }

  async function confirmWarehousePicker() {
    if (!warehousePickerSelected || !warehousePickerProductIds.length) return
    const mode = warehousePickerMode
    const productIds = [...warehousePickerProductIds]
    const sourceRef = warehousePickerSelected
    setWarehousePickerOpen(false)
    setWarehousePickerProductIds([])
    setWarehousePickerCandidates([])
    setWarehousePickerComponents([])
    setWarehousePickerSelected('')
    if (mode === 'export') {
      await exportTo1C(sourceRef, productIds)
    } else {
      await createMaterialIssues(sourceRef, productIds)
    }
  }

  async function exportTo1C(sourceWarehouseRef?: string, productIds?: number[]) {
    const ids = productIds ?? Array.from(selectedIds)
    if (!ids.length) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    const printWindow = prepareRouteSheetWindow()
    try {
      const issueResult = await requestMaterialIssues(sourceWarehouseRef, ids)
      const selectionRequired = issueResult.selection_required ?? []
      const alreadyOnDestination = issueResult.already_on_destination?.reduce((sum, row) => sum + (row.components?.length ?? 0), 0) ?? 0
      if (selectionRequired.length > 0) {
        closeRouteSheetWindow(printWindow)
        showWarehousePicker(issueResult, 'export', ids)
        setMessage(`${alreadyOnDestination ? `Уже на участке: ${alreadyOnDestination}. ` : ''}Для ${selectionRequired.length} поз. нужно выбрать склад-источник перед выгрузкой в 1С.`)
        await load(offsetRef.current)
        return
      }
      const issueIds = issueIdsFromCreateResult(issueResult)
      if (!issueIds.length) {
        closeRouteSheetWindow(printWindow)
        const errors = issueResult.errors?.length ?? 0
        setMessage(`Запуск в 1С: заявок на перемещение не создано${alreadyOnDestination ? `, уже на участке ${alreadyOnDestination}` : ''}${errors ? `, ошибок ${errors}` : ''}`)
        await load(offsetRef.current)
        return
      }
      const result = await exportMaterialIssuesTo1C(issueIds)
      const parent = (result.parent_orders_export ?? {}) as Record<string, unknown>
      const ordersCreated = Number(parent.orders_created ?? 0)
      const ordersExisting = Number(parent.orders_already_linked ?? 0)
      const transfersCreated = Number(result.issues_created ?? 0)
      const transfersExisting = Number(result.issues_already_linked ?? 0)
      const errored = Number(result.issues_error ?? 0) + Number(parent.orders_error ?? 0)
      const skipped = (result.skipped_rows as unknown[])?.length ?? 0
      const summary =
        `Запуск в 1С: заказов проведено ${ordersCreated}` +
        (ordersExisting ? `, заказов уже было ${ordersExisting}` : '') +
        `; перемещений создано ${transfersCreated}` +
        (transfersExisting ? `, перемещений уже было ${transfersExisting}` : '') +
        (skipped ? `, пропущено ${skipped}` : '') +
        (errored ? `, ошибок ${errored}` : '')
      if (errored > 0 || result.status === 'partial_error' || parent.status === 'partial_error') {
        const detail = firstExportProblem(result, parent)
        throw new Error(`${summary}${detail ? `. ${detail}` : ''}`)
      }
      setMessage(
        printWindow
          ? `${summary}. Открыта печать маршрутных листов.`
          : `${summary}. Печать не открыта: браузер заблокировал окно.`,
      )
      await load(offsetRef.current)
      renderRouteSheets(ids, printWindow)
    } catch (e) {
      closeRouteSheetWindow(printWindow)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setLoading(false)
    }
  }

  async function syncFrom1C() {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      // Pulls Posted=true flag from 1C for previously exported transfers and
      // advances local status: К перемещению → Собран.
      const result = await syncPostedTransfers()
      const candidates = Number(result.candidates ?? 0)
      const advanced = Number(result.advanced ?? 0)
      const errors = Array.isArray(result.errors) ? result.errors.length : 0
      setMessage(`Синхронизация: проверено ${candidates}, переведено в «Собран» ${advanced}${errors ? `, ошибок ${errors}` : ''}`)
      if (advanced > 0) {
        await load(offsetRef.current)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function saveOptimalBatch(itemId: number, value: number | null) {
    await updateItem(itemId, {
      optimal_batch: value,
    })
    setRows((list) => list.map((row) => row.item_id === itemId ? { ...row, optimal_batch: value } : row))
  }

  async function saveOrderQuantity(productId: number, value: number) {
    const result = await updateOrderQuantity(productId, value)
    setRows((list) => list.map((row) => row.product_id === productId ? {
      ...row,
      quantity: Number(result.quantity ?? value),
      remaining_qty: Number(result.remaining_qty ?? value),
      mrp_req_net_qty: result.mrp_req_net_qty ?? row.mrp_req_net_qty,
      mrp_req_covered_qty: result.mrp_req_covered_qty ?? row.mrp_req_covered_qty,
      mrp_req_remaining_qty: result.mrp_req_remaining_qty ?? row.mrp_req_remaining_qty,
    } : row))
  }

  async function produceActiveLine(productId: number | null | undefined) {
    if (!productId) return
    const activeOrder = rows.find((row) => row.product_id === productId)
    const qty = Number(activeOrder?.remaining_qty ?? 0)
    if (!Number.isFinite(qty) || qty <= 0) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await produceOrderLine(productId, {
        qty,
        executor: 'erp-shell',
      })
      setMessage(
        `Сборка запасов и сдельный наряд созданы в 1С на ${result.qty} ед. ` +
        'Факт ожидает считывания проведения в Item Ledger.',
      )
      await loadMaterials(productId)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setLoading(false)
    }
  }

  async function returnActiveLeftovers(productId: number | null | undefined) {
    if (!productId) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await returnLeftoverComponents(productId, 'erp-shell')
      const created = Number(result.created_issues ?? 0)
      const skipped = (result.skipped_rows ?? []).length
      setMessage(`Возврат остатков: создано заявок ${created}${skipped ? `, пропущено ${skipped}` : ''}`)
      await loadMaterials(productId)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setLoading(false)
    }
  }

  async function deleteSelectedLocalOrders() {
    const selected = rows.filter((row) => selectedIds.has(row.product_id))
    const deletable = selected.filter((row) => !row.order_ref1c)
    if (!deletable.length) return
    const names = deletable.map((row) => row.order_prodplan_number || row.order_number).join(', ')
    if (!beginDangerousMutation()) return
    if (!window.confirm(`Удалить локальные заказы без 1С: ${names}?`)) {
      endDangerousMutation()
      return
    }
    setLoading(true)
    setError('')
    setMessage('')
    try {
      for (const row of deletable) {
        await deleteProductionOrder(row.product_id)
      }
      setSelectedIds(new Set())
      setMessage(`Удалено локальных заказов: ${deletable.length}`)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setLoading(false)
    }
  }

  function toggleSort(key: ProductionOrderSortKey) {
    const next = nextProductionSort(filtersRef.current, key)
    filtersRef.current = next
    setFilters(next)
    void load(0)
  }

  function changeFilters(next: ProductionFilters, submit = false) {
    filtersRef.current = next
    setFilters(next)
    if (submit) void load(0)
  }

  useEffect(() => {
    void load(0)
    void loadResources()
  }, [load, loadResources])

  useEffect(() => {
    setMaterials(null)
  }, [activeRow?.product_id])

  useEffect(() => {
    const productId = activeRow?.product_id
    if (productId) void loadMaterials(productId)
  }, [activeRow?.product_id, loadMaterials])

  const { visibleFrom, visibleTo } = productionPagination(offset, rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Журнал заказов на производство</div>
        <div className="runBadge">
          MRP run: {runId ?? (loading ? 'загрузка…' : error ? 'недоступен' : '—')}
        </div>
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
          rows={rows}
          selectedIds={selectedIds}
          loading={loading}
          onExportTo1C={() => void exportTo1C()}
          onSyncFrom1C={() => void syncFrom1C()}
          onProduce={() => void produceActiveLine(selectedRows[0]?.product_id)}
          onPrintSelected={() => openRouteSheets(Array.from(selectedIds))}
          onDeleteSelected={() => void deleteSelectedLocalOrders()}
          onOpenSettings={() => void openSettings()}
          onRefresh={() => void load(offset)}
          onSelectAll={() => setSelectedIds(new Set(rows.map((row) => row.product_id)))}
          onClearSelection={() => setSelectedIds(new Set())}
          rootProductLabel={rootProductLabel(rootOptions, filters.root_item_id ? Number(filters.root_item_id) : null)}
          onOpenRootProductFilter={() => setRootDialogOpen(true)}
        />

        {error && rows.length > 0 && <div className="errorLine" role="alert">{error}</div>}
        {message && <div className="successLine" role="status">{message}</div>}

        <ProductionViewBar filters={filters} onChange={changeFilters} />

        <div className="split">
          <div className="tablePane">
            <ProductionFilterBar
              filters={filters}
              resources={resources}
              onChange={changeFilters}
              onSubmit={() => void load(0)}
              onToggleSort={toggleSort}
            />
            <AsyncState
              loading={loading}
              error={error}
              empty={rows.length === 0}
              loadingLabel="Загрузка журнала производства…"
              emptyLabel="В журнале производства нет заказов"
              onRetry={() => void load(offsetRef.current)}
            >
              <ProductionOrdersTable
                rows={rows}
                activeRow={activeRow}
                selectedIds={selectedIds}
                sort={{
                  sortBy: filters.sort_by === 'planned_start_date' ? filters.sort_by : null,
                  sortDir: filters.sort_dir,
                }}
                onSelectIds={setSelectedIds}
                onActivate={setActiveId}
                onOpenMaterials={(row) => void loadMaterials(row.product_id)}
                onChangeStatus={(row, status) => void changeStatus(row, status)}
                onToggleSort={toggleSort}
              />
            </AsyncState>
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
              onLoadMaterials={() => activeRow && void refreshMaterials(activeRow.product_id)}
              onProduce={() => void produceActiveLine(activeRow?.product_id)}
              onReturnLeftovers={() => void returnActiveLeftovers(activeRow?.product_id)}
              onPrint={() => activeRow && openRouteSheets([activeRow.product_id])}
              onOptimalBatchSave={(itemId, value) => saveOptimalBatch(itemId, value)}
              onQuantitySave={(productId, value) => saveOrderQuantity(productId, value)}
            />
          )}
        </div>
      </DocumentWindow>

      <RootProductFilterDialog
        open={rootDialogOpen}
        options={rootOptions}
        value={filters.root_item_id ? Number(filters.root_item_id) : null}
        onApply={(value) => {
          const next = { ...filtersRef.current, root_item_id: value ? String(value) : '' }
          filtersRef.current = next
          setFilters(next)
          setRootDialogOpen(false)
          void load(0)
        }}
        onClose={() => setRootDialogOpen(false)}
      />
      {warehousePickerOpen && warehousePickerCandidates.length > 0 && (
        <WarehousePickerDialog
          warehousePickerCandidates={warehousePickerCandidates}
          warehousePickerComponents={warehousePickerComponents}
          warehousePickerProductIds={warehousePickerProductIds}
          warehousePickerSelected={warehousePickerSelected}
          setWarehousePickerOpen={setWarehousePickerOpen}
          setWarehousePickerSelected={setWarehousePickerSelected}
          confirmWarehousePicker={confirmWarehousePicker}
        />
      )}
    </main>
  )
}
