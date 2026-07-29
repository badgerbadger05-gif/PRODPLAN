import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  coverageLabels,
  paintWeldChainSidesLabel,
  paintWeldChainStateLabel,
  type MaterialIssueCreatePayload,
  type ControlWarehouse,
  type MaterialIssueCreateResponse,
  type MaterialsResponse,
  type OrderRow,
  type PaintWeldChainSide,
  type PaintWeldChainState,
  type ProductionFilters,
  type WarehouseCandidate,
  type WorkshopWarehouse,
} from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'
import { updateItemOptimalBatch } from '../../services/items'
import { getPeriodPlanMatrix, listPeriodPlans } from '../../services/periodPlan'
import {
  cancelLocalProductionOrder,
  closePaintWeldChain,
  createMaterialIssues as createMaterialIssuesAction,
  exportMaterialIssuesTo1C,
  getOrderMaterials,
  getProductionSettings,
  listProductionOrders,
  listProductionResources,
  printRouteSheets,
  produceOrderLine,
  refreshOrderMaterials,
  returnLeftoverComponents,
  saveProductionSettings,
  syncPostedTransfers,
  updateOrderQuantity,
  updateOrderState,
} from '../../services/productionControl'
import { DocumentWindow } from '../layout/DocumentWindow'
import { RootProductFilterDialog, rootProductLabel, type RootProductOption } from '../RootProductFilterDialog'
import { StatusBar } from '../layout/StatusBar'
import { ProductionCommandBar } from './production-control/ProductionCommandBar'
import { ProductionDetailPane } from './production-control/ProductionDetailPane'
import { ProductionFilterBar } from './production-control/ProductionFilterBar'
import { ProductionOrdersTable } from './production-control/ProductionOrdersTable'
import { ProductionSettingsPane } from './production-control/ProductionSettingsPane'
import type { ProductionOrderSortKey } from './production-control/productionOrdersDoctype'

const limit = 100
const coverageDrivenStatuses = new Set(['shortage', 'partial', 'ready'])

function recordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object') : []
}

// Незакрытая до конца цепочка «окраска↔сварка»: что уже в 1С, что осталось и
// какую строку нужно докатить повторным вызовом того же закрытия.
type ChainResumeNotice = {
  productId: number
  message: string
  chainState: PaintWeldChainState
  postedSides: PaintWeldChainSide[]
  pendingSides: PaintWeldChainSide[]
}

function firstExportProblem(...summaries: Array<Record<string, unknown> | null | undefined>) {
  for (const summary of summaries) {
    if (!summary) continue
    for (const entry of recordArray(summary.entries)) {
      const problem = entry.error || entry.reason
      if (problem) return String(problem)
    }
    for (const row of recordArray(summary.skipped_rows)) {
      const problem = row.error || row.reason
      if (problem) return String(problem)
    }
  }
  return ''
}

export function ProductionControlPage() {
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
    sort_by: 'planned_start_date',
    sort_dir: 'asc',
  })
  const filtersRef = useRef(filters)
  const offsetRef = useRef(offset)
  const [message, setMessage] = useState('')
  const [chainResume, setChainResume] = useState<ChainResumeNotice | null>(null)
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

  const activeRow = useMemo(() => rows.find((row) => row.product_id === activeId) ?? rows[0] ?? null, [rows, activeId])
  const selectedRows = useMemo(() => rows.filter((row) => selectedIds.has(row.product_id)), [rows, selectedIds])
  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await listProductionOrders({
        limit,
        offset: nextOffset,
        focusProductId,
        focusOrderId,
        search: filtersRef.current.search,
        status: filtersRef.current.status,
        workshop_id: filtersRef.current.workshop_id,
        coverage_status: filtersRef.current.coverage_status,
        root_item_id: filtersRef.current.root_item_id,
        sort_by: filtersRef.current.sort_by,
        sort_dir: filtersRef.current.sort_dir,
      })
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
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [focusOrderId, focusProductId])

  const loadResources = useCallback(async () => {
    try {
      setResources(await listProductionResources())
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
        getProductionSettings(),
        resources.length ? Promise.resolve(resources) : listProductionResources(),
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
      const saved = await saveProductionSettings({
        workshop_warehouses: workshopRows
          .map((row) => ({
            resource_id: row.resource_id ?? row.workshop_id,
            workshop_id: row.workshop_id ?? row.resource_id,
            warehouse_ref1c: row.warehouse_ref1c,
            production_warehouse_ref1c: row.production_warehouse_ref1c ?? '',
          }))
          .filter((row) => row.resource_id && row.warehouse_ref1c),
        ignored_warehouses: Array.from(ignoredRefs).map((warehouse_ref1c) => ({ warehouse_ref1c })),
        warehouses,
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

  const loadMaterials = useCallback(async (productId: number, refresh = true) => {
    setActiveId(productId)
    setMaterials(null)
    try {
      const data = refresh
        ? await refreshOrderMaterials(productId)
        : await getOrderMaterials(productId)
      setMaterials(data)
      const coverageStatus = String(data.coverage_status || '')
      if (refresh && coverageStatus) {
        setRows((list) => list.map((item) => {
          if (item.product_id !== productId) return item
          const canApplyMaterialCoverage = (!item.issue_status || item.issue_status === 'not_requested')
            && coverageDrivenStatuses.has(String(item.coverage_status || item.status || ''))
          if (!canApplyMaterialCoverage) return item
          return {
            ...item,
            status: coverageDrivenStatuses.has(String(item.status || '')) ? coverageStatus : item.status,
            coverage_status: coverageStatus,
            coverage_label: data.coverage_label || coverageLabels[coverageStatus] || coverageStatus,
          }
        }))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  async function changeStatus(row: OrderRow, status: string) {
    const previous = row.status
    setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status } : item))
    try {
      await updateOrderState(row.product_id, { status })
    } catch (e) {
      setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status: previous } : item))
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function requestMaterialIssues(sourceWarehouseRef: string | undefined, productIds: number[]) {
    const body: MaterialIssueCreatePayload = { product_ids: productIds, initiated_by: 'erp-shell' }
    if (sourceWarehouseRef) body.source_warehouse_ref1c = sourceWarehouseRef
    return createMaterialIssuesAction(body)
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

  function issueIdsFromCreateResult(result: MaterialIssueCreateResponse) {
    return [
      ...(result.created ?? []).map((row) => row.issue_id),
      ...(result.reused ?? []).map((row) => row.issue_id),
    ].filter(Boolean)
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
    void printRouteSheets(ids, { markPrinted: true, autoPrint: true })
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
      const result = await exportMaterialIssuesTo1C(issueIds, { dry_run: false, allow_production: true })
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
    const saved = await updateItemOptimalBatch(itemId, value)
    setRows((list) => list.map((row) => row.item_id === itemId ? { ...row, optimal_batch: saved.optimal_batch ?? null } : row))
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

  async function produceActiveLine(productId?: number | null) {
    if (!productId) return
    const activeOrder = rows.find((row) => row.product_id === productId)
    const qty = Number(activeOrder?.remaining_qty ?? 0)
    if (!Number.isFinite(qty) || qty <= 0) return
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
      await loadMaterials(productId, false)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function returnActiveLeftovers(productId?: number | null) {
    if (!productId) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await returnLeftoverComponents(productId, 'erp-shell')
      const created = Number(result.created_issues ?? 0)
      const skipped = (result.skipped_rows ?? []).length
      setMessage(`Возврат остатков: создано заявок ${created}${skipped ? `, пропущено ${skipped}` : ''}`)
      await loadMaterials(productId, false)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  // Закрытие возобновляемо: тот же вызов и докатывает недостающие документы,
  // поэтому «Докатить» — это повтор closePaintWeldChain по той же строке.
  async function runChainClose(productId: number) {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await closePaintWeldChain(productId)
      if (result.status === 'partial') {
        setChainResume({
          productId,
          message:
            result.message
            || result.error
            || 'Цепочка проведена в 1С частично. Требуется докат: повторите закрытие цепочки.',
          chainState: result.chain_state,
          postedSides: result.posted_sides ?? [],
          pendingSides: result.pending_sides ?? [],
        })
        await load(offsetRef.current)
        return
      }
      setChainResume(null)
      setSelectedIds(new Set())
      await load(offsetRef.current)
      setMessage(
        'Цепочка закрыта: сборки запасов обоих звеньев и комбинированный сдельный наряд проведены в 1С. ' +
        'Факт ожидает считывания проведения в Item Ledger.',
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function closeSelectedChain() {
    const row = selectedRows.length === 1 ? selectedRows[0] : null
    if (!row?.paint_weld_chain) return
    if (!window.confirm('Закрыть оба заказа цепочки окраска↔сварка одним действием?')) return
    await runChainClose(row.product_id)
  }

  function printRows(ids: number[]) {
    openRouteSheets(ids)
  }

  async function deleteSelectedLocalOrders() {
    const selected = rows.filter((row) => selectedIds.has(row.product_id))
    const deletable = selected.filter((row) => !row.order_ref1c)
    if (!deletable.length) return
    const names = deletable.map((row) => row.order_prodplan_number || row.order_number).join(', ')
    if (!window.confirm(`Удалить локальные заказы без 1С: ${names}?`)) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      for (const row of deletable) {
        await cancelLocalProductionOrder(row.product_id)
      }
      setSelectedIds(new Set())
      setMessage(`Удалено локальных заказов: ${deletable.length}`)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function toggleSort(key: ProductionOrderSortKey) {
    const current = filtersRef.current
    const next = {
      ...current,
      sort_by: key,
      sort_dir: current.sort_by === key && current.sort_dir === 'asc' ? 'desc' : 'asc',
    } satisfies ProductionFilters
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
    if (productId) void loadMaterials(productId, false)
  }, [activeRow?.product_id, loadMaterials])

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
          rows={rows}
          selectedIds={selectedIds}
          loading={loading}
          onExportTo1C={() => void exportTo1C()}
          onSyncFrom1C={() => void syncFrom1C()}
          onCloseChain={() => void closeSelectedChain()}
          onPrintSelected={() => printRows(Array.from(selectedIds))}
          onDeleteSelected={() => void deleteSelectedLocalOrders()}
          onOpenSettings={() => void openSettings()}
          onRefresh={() => void load(offset)}
          onSelectAll={() => setSelectedIds(new Set(rows.map((row) => row.product_id)))}
          onClearSelection={() => setSelectedIds(new Set())}
          rootProductLabel={rootProductLabel(rootOptions, filters.root_item_id ? Number(filters.root_item_id) : null)}
          onOpenRootProductFilter={() => setRootDialogOpen(true)}
        />

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}
        {chainResume && (
          <div className="warningLine chainResumeNotice">
            <div className="chainResumeText">
              <strong>Цепочка окраска↔сварка закрыта не полностью.</strong>
              <span>{chainResume.message}</span>
              <span className="muted">
                Состояние: {paintWeldChainStateLabel(chainResume.chainState)}
                {chainResume.postedSides.length ? ` · в 1С: ${paintWeldChainSidesLabel(chainResume.postedSides)}` : ''}
                {chainResume.pendingSides.length ? ` · осталось: ${paintWeldChainSidesLabel(chainResume.pendingSides)}` : ''}
              </span>
            </div>
            <div className="chainResumeActions">
              <button
                className="primary"
                onClick={() => void runChainClose(chainResume.productId)}
                disabled={loading}
                title="Повторить закрытие цепочки: проведённые документы переиспользуются без дублей"
              >
                Докатить
              </button>
              <button onClick={() => setChainResume(null)} disabled={loading}>Скрыть</button>
            </div>
          </div>
        )}

        <div className="split">
          <div className="tablePane">
            <ProductionFilterBar
              filters={filters}
              resources={resources}
              onChange={changeFilters}
              onSubmit={() => void load(0)}
              onToggleSort={toggleSort}
            />
            <ProductionOrdersTable
              rows={rows}
              activeRow={activeRow}
              selectedIds={selectedIds}
              sort={{ sortBy: filters.sort_by, sortDir: filters.sort_dir }}
              onSelectIds={setSelectedIds}
              onActivate={setActiveId}
              onOpenMaterials={(row) => void loadMaterials(row.product_id)}
              onChangeStatus={(row, status) => void changeStatus(row, status)}
              onToggleSort={toggleSort}
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
              onLoadMaterials={() => activeRow && void loadMaterials(activeRow.product_id)}
              onProduce={() => void produceActiveLine(activeRow?.product_id)}
              onReturnLeftovers={() => void returnActiveLeftovers(activeRow?.product_id)}
              onPrint={() => activeRow && printRows([activeRow.product_id])}
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
        <div className="dialogOverlay" onClick={(e) => { if (e.target === e.currentTarget) setWarehousePickerOpen(false) }}>
          <div className="dialogBox">
            <div className="dialogHeader">Выберите склад-источник материалов</div>
            <div className="dialogBody">
              <p>Найдено несколько складов с остатком ({warehousePickerProductIds.length} поз.). Выберите склад отправитель:</p>
              {warehousePickerComponents.length > 0 && (
                <div className="dialogField">
                  <label>Детали</label>
                  <div className="fieldHint">
                    {warehousePickerComponents.map((component, index) => (
                      <div key={`${component.item_name}-${component.item_article ?? ''}-${index}`}>
                        {component.item_name}{component.item_article ? ` (${component.item_article})` : ''} · нужно {component.required_qty.toLocaleString('ru-RU')}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {warehousePickerCandidates.map((c) => (
                <div key={c.ref1c} className="dialogField" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <input
                    type="radio"
                    id={`wh-${c.ref1c}`}
                    name="warehousePicker"
                    value={c.ref1c}
                    checked={warehousePickerSelected === c.ref1c}
                    onChange={() => setWarehousePickerSelected(c.ref1c)}
                  />
                  <label htmlFor={`wh-${c.ref1c}`}>
                    {c.name}
                    {typeof c.qty === 'number'
                      ? ` (${c.qty.toLocaleString('ru-RU')})`
                      : typeof c.components_covered === 'number' && typeof c.total_components === 'number'
                        ? ` (${c.components_covered}/${c.total_components} компонентов)`
                        : ''}
                  </label>
                </div>
              ))}
            </div>
            <div className="dialogFooter">
              <button onClick={() => setWarehousePickerOpen(false)}>Отмена</button>
              <button className="primary" onClick={() => void confirmWarehousePicker()} disabled={!warehousePickerSelected}>
                Подтвердить
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
