import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  coverageLabels,
  type ControlWarehouse,
  type EmployeeOption,
  type MaterialIssueCreateResponse,
  type MaterialsResponse,
  type OrderRow,
  type ProductionOperationOption,
  type ProductionOperationsResponse,
  type ProductionFilters,
  type WarehouseCandidate,
  type WorkshopWarehouse,
} from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'
import { getPeriodPlanMatrix, listPeriodPlans } from '../../services/periodPlan'
import {
  closePaintWeldChain,
  createOrdersFromMrpRequirements,
  deleteProductionOrder,
  exportManufacturesPieceworkTo1C,
  exportManufacturesTo1C,
  exportMaterialIssuesTo1C,
  fetchRouteSheetsPrintHtml,
  getItem,
  getOrderMaterials,
  getProductionControlSettings,
  listProductionEmployees,
  listProductionOperations,
  listProductionOrders,
  markMaterialIssueAssembled,
  postMaterialIssues,
  produceOrder,
  rollbackManufactureLocal,
  saveProductionControlSettings,
  syncPostedTransfers,
  updateItem,
  updateOrderQuantity,
  updateOrderStatus,
  type PaintWeldChainResult,
} from '../../services/productionControl'
import { listResources } from '../../services/resources'
import { DocumentWindow } from '../layout/DocumentWindow'
import { RootProductFilterDialog } from '../RootProductFilterDialog'
import { rootProductLabel, type RootProductOption } from '../rootProductOptions'
import { StatusBar } from '../layout/StatusBar'
import { ProductionCommandBar } from './production-control/ProductionCommandBar'
import { ProductionDetailPane } from './production-control/ProductionDetailPane'
import { ProductionFilterBar } from './production-control/ProductionFilterBar'
import { ProductionOrdersTable } from './production-control/ProductionOrdersTable'
import { ProductionSettingsPane } from './production-control/ProductionSettingsPane'
import { ProductionViewBar } from './production-control/ProductionViewBar'
import type { ProductionOrderSortKey } from './production-control/productionOrdersDoctype'
import { ChainCloseDialog } from './production-control/ChainCloseDialog'
import { ProduceDialog } from './production-control/ProduceDialog'
import { WarehousePickerDialog } from './production-control/WarehousePickerDialog'
import { firstExportProblem, issueIdsFromCreateResult, limit, recordArray } from './production-control/helpers'
import {
  activeProductionRow,
  areAllOperationExecutorsSelected,
  applyMaterialCoverage,
  buildOperationExecutors,
  buildProductionOrderParams,
  buildProductionSettingsPayload,
  deletableProductionRows,
  nextProductionSort,
  productionPagination,
  productionRow,
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
  const [produceOpen, setProduceOpen] = useState(false)
  const [produceQty, setProduceQty] = useState('')
  const [produceEmployeeRef, setProduceEmployeeRef] = useState('')
  const [produceOperations, setProduceOperations] = useState<ProductionOperationOption[]>([])
  const [produceOperationEmployees, setProduceOperationEmployees] = useState<Record<number, string>>({})
  const [produceOperationsLoading, setProduceOperationsLoading] = useState(false)
  const [employees, setEmployees] = useState<EmployeeOption[]>([])
  const [employeesLoading, setEmployeesLoading] = useState(false)
  const [produceDryRun, setProduceDryRun] = useState(false)
  const [produceSaving, setProduceSaving] = useState(false)
  const [produceError, setProduceError] = useState('')
  const [produceDryRunPayload, setProduceDryRunPayload] = useState<string | null>(null)
  const [produceProductId, setProduceProductId] = useState<number | null>(null)
  const [chainOpen, setChainOpen] = useState(false)
  const [chainRowId, setChainRowId] = useState<number | null>(null)
  const [chainPreview, setChainPreview] = useState<PaintWeldChainResult | null>(null)
  const [chainWeldOps, setChainWeldOps] = useState<ProductionOperationOption[]>([])
  const [chainPaintOps, setChainPaintOps] = useState<ProductionOperationOption[]>([])
  const [chainOperationEmployees, setChainOperationEmployees] = useState<Record<number, string>>({})
  const [chainLoading, setChainLoading] = useState(false)
  const [chainSaving, setChainSaving] = useState(false)
  const [chainError, setChainError] = useState('')
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
  const produceRow = useMemo(() => productionRow(rows, produceProductId), [rows, produceProductId])
  const selectedEmployee = useMemo(
    () => employees.find((employee) => employee.employee_ref1c === produceEmployeeRef) ?? null,
    [employees, produceEmployeeRef],
  )
  const allOperationExecutorsSelected = useMemo(
    () => areAllOperationExecutorsSelected(produceOperations, produceOperationEmployees),
    [produceOperations, produceOperationEmployees],
  )
  const produceRemainingQty = Number(produceRow?.remaining_qty ?? 0)
  const produceRequestedQty = Number(produceQty || 0)
  const produceOverageQty = produceRequestedQty - produceRemainingQty
  const canProduceRow = Boolean(produceRow && produceRemainingQty > 0)

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

  const loadEmployees = useCallback(async () => {
    setEmployeesLoading(true)
    try {
      const data = await listProductionEmployees()
      setEmployees(data.rows ?? [])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setEmployeesLoading(false)
    }
  }, [])

  const loadProduceOperations = useCallback(async (productId: number) => {
    setProduceOperationsLoading(true)
    try {
      const data = await listProductionOperations(productId)
      setProduceOperations(data.rows ?? [])
      setProduceOperationEmployees({})
    } catch (e) {
      setProduceOperations([])
      setProduceOperationEmployees({})
      setProduceError(e instanceof Error ? e.message : String(e))
    } finally {
      setProduceOperationsLoading(false)
    }
  }, [])

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

  const loadMaterials = useCallback(async (productId: number, refresh = false) => {
    const requestSeq = ++materialsRequestSeq.current
    setActiveId(productId)
    setMaterials(null)
    try {
      const data = await getOrderMaterials(productId, refresh)
      if (requestSeq !== materialsRequestSeq.current) return
      setMaterials(data)
      const coverageStatus = String(data.coverage_status || '')
      if (refresh && coverageStatus) {
        setRows((list) => applyMaterialCoverage(list, productId, coverageStatus, data.coverage_label))
      }
    } catch (e) {
      if (requestSeq !== materialsRequestSeq.current) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

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
    if (!beginDangerousMutation()) return
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
      endDangerousMutation()
      setLoading(false)
    }
  }

  function openProduceDialog() {
    if (selectedRows.length !== 1) {
      setError('Для выпуска выберите ровно одну строку чекбоксом.')
      setProduceOpen(false)
      return
    }
    const row = selectedRows[0]
    setActiveId(row.product_id)
    setProduceProductId(row.product_id)
    const remaining = Number(row.remaining_qty ?? 0)
    if (remaining <= 0) {
      setError('Эта строка уже произведена полностью. Открывать выпуск нечего.')
      setProduceOpen(false)
      return
    }
    if (row.coverage_status !== 'assembled' && row.issue_status !== 'posted') {
      setError('Нельзя создать выпуск: по выбранной строке нет проведённого перемещения материалов.')
      setProduceOpen(false)
      return
    }
    setProduceQty(String(row.remaining_qty ?? row.quantity ?? 0))
    setProduceEmployeeRef('')
    setProduceOperations([])
    setProduceOperationEmployees({})
    setProduceDryRun(false)
    setProduceError('')
    setProduceDryRunPayload(null)
    setProduceOpen(true)
    void loadEmployees()
    void loadProduceOperations(row.product_id)
  }

  async function openChainDialog() {
    if (selectedRows.length !== 1 || !selectedRows[0].paint_weld_chain) {
      setError('Выберите одну строку цепочки окраска↔сварка.')
      return
    }
    const row = selectedRows[0]
    setActiveId(row.product_id)
    setChainRowId(row.product_id)
    setChainPreview(null)
    setChainWeldOps([])
    setChainPaintOps([])
    setChainOperationEmployees({})
    setChainError('')
    setChainOpen(true)
    setChainLoading(true)
    try {
      const preview = await closePaintWeldChain({ product_id: row.product_id, dry_run: true })
      setChainPreview(preview)
      void loadEmployees()
      const weldProductId = Number(preview?.weld?.product_id)
      const paintProductId = Number(preview?.paint?.product_id)
      const [weldOps, paintOps] = await Promise.all([
        weldProductId && Number(preview?.weld?.qty_to_produce ?? 0) > 0
          ? listProductionOperations(weldProductId)
          : Promise.resolve({ rows: [], total: 0 } as ProductionOperationsResponse),
        paintProductId && Number(preview?.paint?.qty_to_produce ?? 0) > 0
          ? listProductionOperations(paintProductId)
          : Promise.resolve({ rows: [], total: 0 } as ProductionOperationsResponse),
      ])
      setChainWeldOps(weldOps.rows ?? [])
      setChainPaintOps(paintOps.rows ?? [])
    } catch (e) {
      setChainError(e instanceof Error ? e.message : String(e))
    } finally {
      setChainLoading(false)
    }
  }

  function chainExecutorRows(operations: ProductionOperationOption[]) {
    return buildOperationExecutors(operations, chainOperationEmployees)
  }

  async function submitChainClose() {
    if (!chainRowId) return
    if (!beginDangerousMutation()) return
    setChainSaving(true)
    setChainError('')
    try {
      const result = await closePaintWeldChain({
        product_id: chainRowId,
        dry_run: false,
        allow_production: true,
        weld_operation_executors: chainWeldOps.length ? chainExecutorRows(chainWeldOps) : undefined,
        paint_operation_executors: chainPaintOps.length ? chainExecutorRows(chainPaintOps) : undefined,
        initiated_by: 'erp-shell-chain-close',
      })
      const piecework = result.piecework_export ?? {}
      if (result.status !== 'ok' || (piecework.status && !['ok', 'existing'].includes(String(piecework.status)))) {
        const detail = String(piecework.error ?? '') || firstExportProblem(piecework, result.manufactures_export as Record<string, unknown>)
        throw new Error(`Цепочка закрыта частично.${detail ? ` ${detail}` : ''}`)
      }
      const ref = String(piecework.target_ref_key ?? '')
      setMessage(
        `Цепочка закрыта: комбинированный сдельный${ref ? ` (${ref.slice(0, 8)}…)` : ''}, оба заказа завершены в 1С.`,
      )
      setChainOpen(false)
      setSelectedIds(new Set())
      await load(offsetRef.current)
    } catch (e) {
      setChainError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setChainSaving(false)
    }
  }

  async function submitProduce() {
    if (!produceRow) return
    if (Number(produceRow.remaining_qty ?? 0) <= 0) {
      setError('Эта строка уже произведена полностью. Открывать выпуск нечего.')
      setProduceOpen(false)
      return
    }
    if (produceRow.coverage_status !== 'assembled' && produceRow.issue_status !== 'posted') {
      setError('Нельзя создать выпуск: по выбранной строке нет проведённого перемещения материалов.')
      setProduceOpen(false)
      return
    }
    if (!beginDangerousMutation()) return
    setProduceSaving(true)
    setError('')
    setProduceError('')
    setProduceDryRunPayload(null)
    let manufactureIdToRollback: number | null = null
    let manufactureExportedRef = ''
    try {
      const requestedQty = Number(produceQty) || 0
      const currentRemaining = Number(produceRow.remaining_qty ?? 0)
      const overageQty = requestedQty - currentRemaining
      if (overageQty > 0.000001) {
        if (produceDryRun) {
          throw new Error('dry_run для выпуска больше плана пока недоступен: сначала нужно создать и провести дополнительное перемещение.')
        }
        const targetQuantity = Number(produceRow.produced_qty ?? 0) + requestedQty
        await updateOrderQuantity(produceRow.product_id, targetQuantity)
        const issueResult = await postMaterialIssues([produceRow.product_id], 'erp-shell-overproduction')
        const selectionRequired = issueResult.selection_required ?? []
        if (selectionRequired.length > 0) {
          throw new Error('Для дополнительного перемещения на разницу нужно выбрать склад-источник материалов.')
        }
        // direction='in_place' — локальный резерв компонентов, уже лежащих на
        // участке: в 1С не выгружается и не проводится.
        const physicalRows = [
          ...(issueResult.created ?? []),
          ...(issueResult.reused ?? []),
        ].filter((row) => row.issue_id && row.direction !== 'in_place')
        const claimedInPlace = (issueResult.created ?? []).some((row) => row.direction === 'in_place')
        const issueIds = physicalRows.map((row) => row.issue_id)
        if (!issueIds.length && !claimedInPlace) {
          const detail = issueResult.errors?.join('; ')
          throw new Error(`Не удалось создать дополнительное перемещение на разницу ${overageQty}.${detail ? ` ${detail}` : ''}`)
        }
        if (issueIds.length) {
          const transferResult = await exportMaterialIssuesTo1C(issueIds)
          const transferErrors = Number(transferResult.issues_error ?? 0)
          const transferCreated = Number(transferResult.issues_created ?? 0)
          const transferExisting = Number(transferResult.issues_already_linked ?? 0)
          if (transferErrors > 0 || transferCreated + transferExisting < 1 || transferResult.status === 'partial_error') {
            const detail = firstExportProblem(transferResult, transferResult.parent_orders_export as Record<string, unknown> | undefined)
            throw new Error(`Дополнительное перемещение на разницу не выгружено в 1С.${detail ? ` ${detail}` : ''}`)
          }
          for (const issueId of issueIds) {
            await markMaterialIssueAssembled(issueId)
          }
        }
      }

      // Step 1: record manufacture locally (bumps produced_qty / remaining_qty).
      const operationExecutors = buildOperationExecutors(produceOperations, produceOperationEmployees, employees)
      const localResult = await produceOrder(produceRow.product_id, {
        qty: requestedQty,
        executor: produceOperations.length ? undefined : selectedEmployee?.employee_name || undefined,
        operation_executors: produceOperations.length ? operationExecutors : undefined,
      })
      const manufacture_id = Number(localResult.manufacture_id)
      manufactureIdToRollback = manufacture_id

      if (produceDryRun) {
        const dryRunResult = await exportManufacturesTo1C([manufacture_id], true, false)
        await rollbackManufactureLocal(manufacture_id)
        setProduceDryRunPayload(JSON.stringify(dryRunResult, null, 2))
        await load(offsetRef.current)
        return
      }

      // Step 2: export the manufacture to 1C as Document_СборкаЗапасов (Posted=false).
      const exportResult = await exportManufacturesTo1C([manufacture_id], false, true)
      const created1c = Number(exportResult.manufactures_created ?? 0)
      const errored = Number(exportResult.manufactures_error ?? 0)
      const exportEntry = recordArray(exportResult.entries)[0]
      const ref = exportEntry?.target_ref_key
      manufactureExportedRef = typeof ref === 'string' ? ref : ''
      if (errored > 0 || created1c < 1 || !ref) {
        const exportError = exportEntry?.error || exportEntry?.reason || firstExportProblem(exportResult)
        if (!ref) {
          await rollbackManufactureLocal(manufacture_id)
          throw new Error(String(exportError || '1C не создала документ выпуска; локальный выпуск откатан'))
        }
        throw new Error(
          `1C создала документ выпуска ${String(ref).slice(0, 8)}…, но не провела его: ` +
          `${String(exportError || 'ошибка проведения')}. Локальный выпуск оставлен для разбора.`,
        )
      }
      const pieceworkResult = await exportManufacturesPieceworkTo1C([manufacture_id])
      const pieceworkCreated = Number(pieceworkResult.manufactures_created ?? 0)
      const pieceworkErrored = Number(pieceworkResult.manufactures_error ?? 0)
      const pieceworkEntry = ((pieceworkResult.entries as Array<Record<string, unknown>>) ?? [])[0]
      if (pieceworkErrored > 0 || pieceworkCreated < 1 || !pieceworkEntry?.target_ref_key) {
        const exportError = pieceworkEntry?.error || pieceworkEntry?.reason || '1C не создала сдельный наряд'
        throw new Error(`Производство создано в 1С, но сдельный наряд не создан: ${String(exportError)}`)
      }
      const manufactureNumber = String(((exportResult.entries as Array<Record<string, unknown>>) ?? [])[0]?.number ?? '')
      const pieceworkNumber = String(pieceworkEntry.number ?? '')
      setMessage(
        `Создано в 1С: производство ${manufactureNumber || String(ref).slice(0, 8)} ` +
        `(${String(ref).slice(0, 8)}…), сдельный наряд ${pieceworkNumber || String(pieceworkEntry.target_ref_key).slice(0, 8)} ` +
        `(${String(pieceworkEntry.target_ref_key).slice(0, 8)}…). ` +
        `Произведено: ${localResult.qty}, осталось: ${localResult.remaining_qty}.`,
      )
      setProduceOpen(false)
      setSelectedIds(new Set())
      await load(offsetRef.current)
    } catch (e) {
      if (manufactureIdToRollback && !manufactureExportedRef) {
        try {
          await rollbackManufactureLocal(manufactureIdToRollback)
          await load(offsetRef.current)
        } catch {
          // Keep the original 1C error visible to the operator.
        }
      }
      const text = e instanceof Error ? e.message : String(e)
      setError(text)
      setProduceError(text)
      if (text.includes('remaining_qty=0') || text.includes('уже произведена полностью')) {
        setProduceOpen(false)
        await load(offsetRef.current)
      }
    } finally {
      endDangerousMutation()
      setProduceSaving(false)
    }
  }

  async function fillRemaining(_runId: number, requirementId: number) {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await createOrdersFromMrpRequirements([requirementId], 'erp-shell')
      const created = Number(result.created_count ?? (Array.isArray(result.created) ? result.created.length : 0))
      const existing = Number(result.existing_count ?? (Array.isArray(result.reused) ? result.reused.length : 0))
      const skipped = Number(Array.isArray(result.skipped) ? result.skipped.length : 0)
      setMessage(`Досоздано: новых ${created}, уже было ${existing}${skipped ? `, пропущено ${skipped}` : ''}`)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function saveOptimalBatch(itemId: number, value: number | null) {
    const item = await getItem(itemId)
    await updateItem(itemId, {
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

  function printRows(ids: number[]) {
    openRouteSheets(ids)
  }

  async function deleteSelectedLocalOrders() {
    const deletable = deletableProductionRows(rows, selectedIds)
    if (!deletable.length) return
    if (!beginDangerousMutation()) return
    const names = deletable.map((row) => row.order_prodplan_number || row.order_number).join(', ')
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
    const current = filtersRef.current
    const next = nextProductionSort(current, key)
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

  const { visibleFrom, visibleTo } = productionPagination(offset, rows.length, total)

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
          onProduce={() => openProduceDialog()}
          onCloseChain={() => void openChainDialog()}
          onPrintSelected={() => printRows(Array.from(selectedIds))}
          onDeleteSelected={() => void deleteSelectedLocalOrders()}
          onOpenSettings={() => void openSettings()}
          onRefresh={() => void load(offset)}
          onSelectAll={() => setSelectedIds(new Set(rows.map((row) => row.product_id)))}
          onClearSelection={() => setSelectedIds(new Set())}
          rootProductLabel={rootProductLabel(rootOptions, filters.root_item_id ? Number(filters.root_item_id) : null)}
          onOpenRootProductFilter={() => setRootDialogOpen(true)}
        />

        {error && <div className="errorLine" role="alert">{error}</div>}
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
              onOpenMaterials={(row) => void loadMaterials(row.product_id, false)}
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
              onLoadMaterials={() => activeRow && void loadMaterials(activeRow.product_id, true)}
              onPrint={() => activeRow && printRows([activeRow.product_id])}
              onOptimalBatchSave={(itemId, value) => saveOptimalBatch(itemId, value)}
              onQuantitySave={(productId, value) => saveOrderQuantity(productId, value)}
              onFillRemaining={(sourceRunId, requirementId) => fillRemaining(sourceRunId, requirementId)}
            />
          )}
        </div>
      </DocumentWindow>

      {produceOpen && produceRow && (
        <ProduceDialog
          produceRow={produceRow}
          produceError={produceError}
          canProduceRow={canProduceRow}
          produceQty={produceQty}
          setProduceQty={setProduceQty}
          produceSaving={produceSaving}
          produceOverageQty={produceOverageQty}
          produceOperations={produceOperations}
          produceOperationEmployees={produceOperationEmployees}
          setProduceOperationEmployees={setProduceOperationEmployees}
          employees={employees}
          employeesLoading={employeesLoading}
          produceOperationsLoading={produceOperationsLoading}
          produceEmployeeRef={produceEmployeeRef}
          setProduceEmployeeRef={setProduceEmployeeRef}
          produceDryRun={produceDryRun}
          setProduceDryRun={setProduceDryRun}
          setProduceDryRunPayload={setProduceDryRunPayload}
          produceDryRunPayload={produceDryRunPayload}
          allOperationExecutorsSelected={allOperationExecutorsSelected}
          setProduceOpen={setProduceOpen}
          submitProduce={submitProduce}
        />
      )}
      {chainOpen && (
        <ChainCloseDialog
          chainSaving={chainSaving}
          setChainOpen={setChainOpen}
          chainError={chainError}
          chainLoading={chainLoading}
          chainPreview={chainPreview}
          chainWeldOps={chainWeldOps}
          chainPaintOps={chainPaintOps}
          chainOperationEmployees={chainOperationEmployees}
          setChainOperationEmployees={setChainOperationEmployees}
          employees={employees}
          employeesLoading={employeesLoading}
          submitChainClose={submitChainClose}
        />
      )}
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
