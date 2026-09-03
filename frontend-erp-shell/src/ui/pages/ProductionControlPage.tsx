import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  coverageLabels,
  type ControlWarehouse,
  type EmployeeOption,
  type MaterialIssueCreateResponse,
  type MaterialsResponse,
  type OrderRow,
  type ProductionFilters,
  type ProductionOperationOption,
  type WarehouseCandidate,
  type WorkshopWarehouse,
} from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'
import {
  deleteProductionOrder,
  exportMaterialIssuesTo1C,
  fetchRouteSheetsPrintHtml,
  closeProductionOrder,
  getOrderMaterials,
  getWorkItemMaterials,
  getProductionControlSettings,
  listRootProductOptions,
  listProductionEmployees,
  listProductionOperations,
  listProductionOrders,
  materializeMakeWorkItems,
  updateOrderQuantity,
  openPaintWeldChains,
  closePaintWeldChain,
  postMaterialIssues,
  produceOrderLine,
  returnLeftoverComponents,
  saveProductionControlSettings,
  syncExecutionFrom1C,
  updateItem,
  updateOrderStatus,
} from '../../services/productionControl'
import { listResources } from '../../services/resources'
import { DocumentWindow } from '../layout/DocumentWindow'
import { RootProductFilterDialog } from '../RootProductFilterDialog'
import { rootProductLabel, type RootProductOption } from '../rootProductOptions'
import { StatusBar } from '../layout/StatusBar'
import { TruthBadge } from '../layout/TruthBadge'
import {
  truthBadgeMetaFromApiError,
  type TruthBadgeMeta,
} from '../../services/planningTruth'
import { AsyncState } from '../layout/AsyncState'
import { ProductionCommandBar } from './production-control/ProductionCommandBar'
import { ProductionDetailPane } from './production-control/ProductionDetailPane'
import { ProductionFilterBar } from './production-control/ProductionFilterBar'
import { ProductionOrdersTable } from './production-control/ProductionOrdersTable'
import { ProductionSettingsPane } from './production-control/ProductionSettingsPane'
import { ProductionViewBar } from './production-control/ProductionViewBar'
import type { ProductionOrderSortKey } from './production-control/productionOrdersDoctype'
import { ProduceDialog, type ProduceChainSide } from './production-control/ProduceDialog'
import { WarehousePickerDialog } from './production-control/WarehousePickerDialog'
import { firstExportProblem, issueIdsFromCreateResult, limit } from './production-control/helpers'
import {
  activeProductionRow,
  buildProductionOrderParams,
  buildProductionSettingsPayload,
  DEFAULT_PRODUCTION_FILTERS,
  nextProductionSort,
  parseProductionControlUrlState,
  productionPagination,
  productionRowId,
  productionRowProductIds,
  selectedProductionRows,
  writeProductionControlUrlState,
} from './production-control/model'

export function ProductionControlPage() {
  const listRequestSeq = useRef(0)
  const materialsRequestSeq = useRef(0)
  const dangerousMutationLocked = useRef(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const focusProductId = searchParams.get('product_id')
  const focusOrderId = searchParams.get('order_id')
  const initialUrlState = useRef(parseProductionControlUrlState(searchParams))
  const [rows, setRows] = useState<OrderRow[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [activeId, setActiveId] = useState<number | null>(
    initialUrlState.current.activeProductId,
  )
  const [materials, setMaterials] = useState<MaterialsResponse | null>(null)
  const [launchQtyByWorkItem, setLaunchQtyByWorkItem] = useState<Record<number, number>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [total, setTotal] = useState(0)
  const [runId, setRunId] = useState<number | null>(null)
  const [truthMeta, setTruthMeta] = useState<TruthBadgeMeta | null>(null)
  const [rootOptions, setRootOptions] = useState<RootProductOption[]>([])
  const [rootDialogOpen, setRootDialogOpen] = useState(false)
  const [offset, setOffset] = useState(initialUrlState.current.offset)
  const [filters, setFilters] = useState<ProductionFilters>(
    initialUrlState.current.filters ?? DEFAULT_PRODUCTION_FILTERS,
  )
  const filtersRef = useRef(filters)
  const offsetRef = useRef(offset)
  const [message, setMessage] = useState('')
  const [resources, setResources] = useState<ProductionResource[]>([])
  const [settingsOpen, setSettingsOpen] = useState(false)
  // Диалог «Произвести»: 1С не проведёт сдельный наряд без исполнителя, поэтому
  // операции выбираются поимённо до отправки, а не подставляются заглушкой.
  const [produceOpen, setProduceOpen] = useState(false)
  const [produceQty, setProduceQty] = useState('')
  const [produceSaving, setProduceSaving] = useState(false)
  const [produceError, setProduceError] = useState('')
  const [employees, setEmployees] = useState<EmployeeOption[]>([])
  const [employeesLoading, setEmployeesLoading] = useState(false)
  const [produceOperations, setProduceOperations] = useState<ProductionOperationOption[]>([])
  const [produceOperationsLoading, setProduceOperationsLoading] = useState(false)
  const [produceOperationEmployees, setProduceOperationEmployees] = useState<Record<number, string>>({})
  const [produceEmployeeRef, setProduceEmployeeRef] = useState('')
  // Цепочка «сварка → окраска» закрывается одним комбинированным нарядом,
  // поэтому исполнителей выбирают сразу на обе стороны в том же диалоге.
  const [produceChainSides, setProduceChainSides] = useState<ProduceChainSide[] | null>(null)
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

  useEffect(() => {
    setSearchParams(writeProductionControlUrlState(searchParams, {
      filters,
      offset,
      activeProductId: activeId,
    }), { replace: true })
    // URL params are cloned from the current render, but changes to external
    // product_id/order_id must not reset locally owned journal state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, filters, offset, setSearchParams])

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
      setTruthMeta(data.truth_meta)
      setOffset(data.offset ?? nextOffset)
      setActiveId((current) => {
        const focusedProductId = Number(focusProductId || 0)
        if (focusedProductId && data.rows?.some((row) => row.product_id === focusedProductId)) return focusedProductId
        if (current && data.rows?.some((row) => productionRowId(row) === current)) return current
        return data.rows?.[0] ? productionRowId(data.rows[0]) : null
      })
    } catch (e) {
      if (requestSeq !== listRequestSeq.current) return
      setTruthMeta(truthBadgeMetaFromApiError(e))
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
        const data = await listRootProductOptions()
        if (cancelled) return
        setRootOptions((data.rows ?? []).map((row) => row))
      } catch {
        if (!cancelled) setRootOptions([])
      }
    }
    void loadRootOptions()
    return () => { cancelled = true }
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

  const requestMaterials = useCallback(async (
    productId: number,
    request: (id: number) => Promise<MaterialsResponse>,
  ) => {
    const requestSeq = ++materialsRequestSeq.current
    setActiveId(productId)
    setMaterials(null)
    try {
      const data = await request(productId)
      if (requestSeq !== materialsRequestSeq.current) return
      setMaterials(data)
      setRows((list) => list.map((row) => productionRowId(row) === productId ? {
        ...row,
        coverage_status: data.coverage_status || row.coverage_status,
        coverage_label: data.coverage_label || row.coverage_label,
        material_coverage_status: data.coverage_status || row.material_coverage_status,
        material_coverage_label: data.coverage_label || row.material_coverage_label,
      } : row))
    } catch (e) {
      if (requestSeq !== materialsRequestSeq.current) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const loadMaterials = useCallback(
    (productId: number) => requestMaterials(productId, getOrderMaterials),
    [requestMaterials],
  )

  const loadWorkItemMaterials = useCallback(
    (workItemId: number, quantity: number) => requestMaterials(
      -workItemId,
      () => getWorkItemMaterials(workItemId, quantity),
    ),
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
    if (row.product_id == null) return
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
    const ids = productIds ?? selectedRows.flatMap(productionRowProductIds)
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
    let ids = productIds ?? selectedRows.flatMap(productionRowProductIds)
    const workItemIds = productIds == null
      ? selectedRows.flatMap((row) => row.work_item_id == null ? [] : [row.work_item_id])
      : []
    if (!ids.length && !workItemIds.length) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    let printWindow: Window | null = null
    try {
      if (workItemIds.length) {
        const materialized = await materializeMakeWorkItems(workItemIds.map((workItemId) => {
          const row = selectedRows.find((item) => item.work_item_id === workItemId)
          return {
            work_item_id: workItemId,
            launch_qty: launchQtyByWorkItem[workItemId] ?? row?.launchable_qty ?? row?.quantity ?? 0,
            expected_materialized_qty: row?.materialized_order_qty ?? 0,
          }
        }))
        ids = Array.from(new Set([
          ...ids,
          ...(materialized.created ?? []).map((row) => row.product_id),
          ...(materialized.reused ?? []).map((row) => row.product_id),
        ]))
      }
      if (!ids.length) throw new Error('Не удалось создать исполнительные заказы из расчёта MRP')
      const weldedNames = rows
        .filter((row) => row.product_id != null
          && ids.includes(row.product_id)
          && row.paint_weld_pair?.role === 'painted')
        .map((row) => row.paint_weld_pair?.counterpart_item_name)
        .filter((name): name is string => Boolean(name))
      if (weldedNames.length > 0 && !window.confirm(
        `Будет открыта цепочка сварка → окраска. Сначала будет запущена сварная деталь: ${Array.from(new Set(weldedNames)).join(', ')}. Продолжить?`,
      )) {
        return
      }
      const chains = await openPaintWeldChains(ids)
      ids = Array.from(new Set(chains.product_ids ?? ids))
      printWindow = prepareRouteSheetWindow()
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
      const result = await syncExecutionFrom1C()
      const ordersUpdated = Number(result.orders.orders_updated ?? 0)
      const candidates = Number(result.transfers.candidates ?? 0)
      const advanced = Number(result.transfers.advanced ?? 0)
      const orderErrors = Array.isArray(result.orders.errors) ? result.orders.errors.length : 0
      const transferErrors = Array.isArray(result.transfers.errors) ? result.transfers.errors.length : 0
      const errors = orderErrors + transferErrors
      await load(offsetRef.current)
      setMessage(
        `Синхронизация: заказов обновлено ${ordersUpdated}, перемещений проверено ${candidates}, переведено в «Собран» ${advanced}`
        + (errors ? `, ошибок ${errors}` : ''),
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  // Количество уже созданного, но ещё не открытого в 1С заказа. Строку журнала
  // обновляем сразу, комплектацию перечитываем с сервера: потребность
  // компонентов считает бэкенд от нового количества, а не эта страница.
  async function saveOrderQuantity(productId: number, value: number) {
    setError('')
    setMessage('')
    const result = await updateOrderQuantity(productId, value)
    setRows((list) => list.map((row) => row.product_id === productId
      ? { ...row, quantity: result.quantity, remaining_qty: result.remaining_qty }
      : row))
    const openIssues = result.material_issues_open ?? 0
    setMessage(
      `Количество к запуску: ${result.previous_quantity} → ${result.quantity}`
      + (openIssues
        ? `; заявок на перемещение ${openIssues} — будут приведены к новому количеству при следующем запросе материалов`
        : ''),
    )
    await loadMaterials(productId)
  }

  async function saveOptimalBatch(itemId: number, value: number | null) {
    await updateItem(itemId, {
      optimal_batch: value,
    })
    setRows((list) => list.map((row) => row.item_id === itemId ? { ...row, optimal_batch: value } : row))
  }

  // Исполнителей выбирают до записи в 1С: наряд с пустой строкой регистра
  // «Сдельные наряды» 1С не проводит, а документ к тому моменту уже создан.
  async function openProduceDialog(productId: number | null | undefined) {
    if (!productId) return
    const row = rows.find((item) => item.product_id === productId)
    if (!row) return
    const chain = row.paint_weld_chain
    const counterpartProductId = chain?.counterpart_product_id ?? null
    setProduceError('')
    setProduceQty(String(row.remaining_qty ?? row.quantity ?? 0))
    setProduceOperationEmployees({})
    setProduceEmployeeRef('')
    setProduceOperations([])
    setProduceChainSides(null)
    setProduceOpen(true)
    setEmployeesLoading(true)
    setProduceOperationsLoading(true)
    try {
      const [employeeList, operationList, counterpartOperations] = await Promise.all([
        listProductionEmployees(),
        listProductionOperations(productId),
        counterpartProductId ? listProductionOperations(counterpartProductId) : Promise.resolve(null),
      ])
      setEmployees(employeeList.rows ?? [])
      const ownOperations = operationList.rows ?? []
      if (chain && counterpartProductId) {
        const own: ProduceChainSide = {
          key: chain.role === 'welded' ? 'weld' : 'paint',
          title: chain.role === 'welded' ? 'Сварка' : 'Окраска',
          productId,
          itemName: row.item_name,
          qty: row.remaining_qty ?? row.quantity ?? 0,
          unit: row.unit,
          operations: ownOperations,
        }
        const counterpart: ProduceChainSide = {
          key: chain.role === 'welded' ? 'paint' : 'weld',
          title: chain.role === 'welded' ? 'Окраска' : 'Сварка',
          productId: counterpartProductId,
          itemName: chain.counterpart_item_name,
          qty: chain.counterpart_remaining_qty ?? chain.counterpart_quantity ?? 0,
          unit: chain.counterpart_unit,
          operations: counterpartOperations?.rows ?? [],
        }
        // Порядок блоков — порядок цепочки: сварка сначала, окраска следом.
        const sides = own.key === 'weld' ? [own, counterpart] : [counterpart, own]
        setProduceChainSides(sides)
        setProduceOperations(sides.flatMap((side) => side.operations))
      } else {
        setProduceOperations(ownOperations)
      }
    } catch (e) {
      setProduceError(e instanceof Error ? e.message : String(e))
    } finally {
      setEmployeesLoading(false)
      setProduceOperationsLoading(false)
    }
  }

  function operationExecutorsOf(operations: ProductionOperationOption[]) {
    return operations
      .filter((operation) => produceOperationEmployees[operation.spec_operation_id])
      .map((operation) => ({
        spec_operation_id: operation.spec_operation_id,
        operation_id: operation.operation_id,
        line_number: operation.line_number,
        employee_ref1c: produceOperationEmployees[operation.spec_operation_id],
      }))
  }

  async function submitProduce() {
    const productId = activeRow?.product_id
    if (!productId) return
    // Бэкенд ищет исполнителя шапки по имени сотрудника, а список отдаёт
    // ссылки: имя берём из того же списка, чтобы поиск сошёлся.
    const headerExecutor = produceEmployeeRef
      ? employees.find((employee) => employee.employee_ref1c === produceEmployeeRef)?.employee_name
      : undefined
    if (produceChainSides) {
      // Цепочка: количества сторон считает бэкенд по остатку каждой из них,
      // отсюда уходят только исполнители обеих сторон.
      setProduceError('')
      setProduceSaving(true)
      try {
        const weldExecutors = operationExecutorsOf(
          produceChainSides.find((side) => side.key === 'weld')?.operations ?? [],
        )
        const paintExecutors = operationExecutorsOf(
          produceChainSides.find((side) => side.key === 'paint')?.operations ?? [],
        )
        const result = await closePaintWeldChain(productId, {
          ...(headerExecutor ? { executor: headerExecutor } : {}),
          ...(weldExecutors.length ? { weld_operation_executors: weldExecutors } : {}),
          ...(paintExecutors.length ? { paint_operation_executors: paintExecutors } : {}),
        })
        setProduceOpen(false)
        setMessage(
          result.message
            || (result.resume_required
              ? 'Цепочка сварка–окраска закрыта частично. Повторите действие для завершения.'
              : 'Сварка и окраска закрыты совместно; создан один комбинированный сдельный наряд.'),
        )
        await loadMaterials(productId)
        await load(offsetRef.current)
      } catch (e) {
        setProduceError(e instanceof Error ? e.message : String(e))
      } finally {
        setProduceSaving(false)
      }
      return
    }
    const qty = Number(produceQty)
    if (!Number.isFinite(qty) || qty <= 0) {
      setProduceError('Количество должно быть больше нуля')
      return
    }
    setProduceError('')
    setProduceSaving(true)
    try {
      const operationExecutors = operationExecutorsOf(produceOperations)
      const result = await produceOrderLine(productId, {
        qty,
        ...(headerExecutor ? { executor: headerExecutor } : {}),
        ...(operationExecutors.length ? { operation_executors: operationExecutors } : {}),
      })
      setProduceOpen(false)
      setMessage(
        `Сборка запасов и сдельный наряд созданы в 1С на ${result.qty} ед. ` +
        'Факт ожидает считывания проведения в Item Ledger.',
      )
      await loadMaterials(productId)
      await load(offsetRef.current)
    } catch (e) {
      setProduceError(e instanceof Error ? e.message : String(e))
    } finally {
      setProduceSaving(false)
    }
  }

  async function produceActiveLine(productId: number | null | undefined) {
    if (!productId) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      // Оба пути — строка и цепочка «сварка → окраска» — проходят через один
      // диалог: пока хоть одна операция без исполнителя, в 1С ничего не уходит.
      await openProduceDialog(productId)
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

  async function closeActiveOrder(productId: number | null | undefined) {
    if (!productId) return
    const active = rows.find((row) => row.product_id === productId)
    if (!active || !active.available_actions?.includes('close_1c')) return
    if (!beginDangerousMutation()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await closeProductionOrder(productId, { dry_run: false })
      const confirmed = result.status === 'ok'
        && result.dry_run === false
        && result.orders_closed === 1
        && result.orders_error === 0
      await load(offsetRef.current)
      if (confirmed) {
        setMessage(`Заказ закрыт в 1С по кнопке: ${active.order_prodplan_number || active.order_number}`)
      } else {
        setError(
          `Закрытие заказа в 1С не подтверждено: статус ${result.status || 'неизвестен'}, `
          + `закрыто ${result.orders_closed ?? 0}, ошибок ${result.orders_error ?? 0}`,
        )
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      endDangerousMutation()
      setLoading(false)
    }
  }

  async function deleteSelectedLocalOrders() {
    const selected = rows.filter(
      (row) => row.product_id != null && selectedIds.has(productionRowId(row)) && !row.order_ref1c,
    )
    if (!selected.length) return
    const names = selected.map((row) => row.order_prodplan_number || row.order_number).join(', ')
    if (!beginDangerousMutation()) return
    if (!window.confirm(`Удалить локальные заказы без 1С: ${names}?`)) {
      endDangerousMutation()
      return
    }
    setLoading(true)
    setError('')
    setMessage('')
    try {
      for (const row of selected) {
        if (row.product_id != null) await deleteProductionOrder(row.product_id)
      }
      setSelectedIds(new Set())
      setMessage(`Удалено локальных заказов: ${selected.length}`)
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
    void load(offsetRef.current)
    void loadResources()
  }, [load, loadResources])

  useEffect(() => {
    setMaterials(null)
  }, [activeRow?.journal_row_key])

  useEffect(() => {
    const productId = activeRow?.product_id
    const workItemId = activeRow?.work_item_id
    if (productId) {
      void loadMaterials(productId)
    } else if (workItemId) {
      const launchQty = launchQtyByWorkItem[workItemId]
        ?? activeRow.launchable_qty
        ?? activeRow.quantity
      const timer = window.setTimeout(() => void loadWorkItemMaterials(workItemId, launchQty), 250)
      return () => window.clearTimeout(timer)
    }
    return undefined
  }, [activeRow?.journal_row_key, activeRow?.product_id, activeRow?.work_item_id, activeRow?.launchable_qty, activeRow?.quantity, launchQtyByWorkItem, loadMaterials, loadWorkItemMaterials])

  const { visibleFrom, visibleTo } = productionPagination(offset, rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Журнал заказов на производство</div>
        <div className="runBadge">
          MRP run: {runId ?? (loading ? 'загрузка…' : error ? 'недоступен' : '—')}
        </div>
        <TruthBadge meta={truthMeta} />
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
          canClose={selectedRows.length === 1 && selectedRows[0].available_actions.includes('close_1c')}
          loading={loading}
          onExportTo1C={() => void exportTo1C()}
          onSyncFrom1C={() => void syncFrom1C()}
          onProduce={() => void produceActiveLine(selectedRows[0]?.product_id)}
          onClose={() => void closeActiveOrder(selectedRows[0]?.product_id)}
          onPrintSelected={() => openRouteSheets(selectedRows.flatMap(productionRowProductIds))}
          onDeleteSelected={() => void deleteSelectedLocalOrders()}
          onOpenSettings={() => void openSettings()}
          onRefresh={() => void load(offset)}
          onSelectAll={() => setSelectedIds(new Set(rows
            .filter((row) => !row.selection_disabled_reason)
            .map(productionRowId)))}
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
                launchQtyByWorkItem={launchQtyByWorkItem}
                sort={{
                  sortBy: filters.sort_by === 'planned_start_date' ? filters.sort_by : null,
                  sortDir: filters.sort_dir,
                }}
                onSelectIds={setSelectedIds}
                onActivate={setActiveId}
                onOpenMaterials={(row) => { if (row.product_id != null) void loadMaterials(row.product_id) }}
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
              onLoadMaterials={() => {
                if (activeRow?.product_id != null) void loadMaterials(activeRow.product_id)
                else if (activeRow?.work_item_id != null) {
                  void loadWorkItemMaterials(
                    activeRow.work_item_id,
                    launchQtyByWorkItem[activeRow.work_item_id] ?? activeRow.launchable_qty ?? activeRow.quantity,
                  )
                }
              }}
              launchQuantity={activeRow?.work_item_id != null
                ? launchQtyByWorkItem[activeRow.work_item_id] ?? activeRow.launchable_qty ?? activeRow.quantity
                : null}
              onLaunchQuantityChange={(value) => {
                if (activeRow?.work_item_id == null) return
                setLaunchQtyByWorkItem((current) => ({ ...current, [activeRow.work_item_id as number]: value }))
              }}
              onProduce={() => void produceActiveLine(activeRow?.product_id)}
              onReturnLeftovers={() => void returnActiveLeftovers(activeRow?.product_id)}
              onPrint={() => { if (activeRow) openRouteSheets(productionRowProductIds(activeRow)) }}
              onOptimalBatchSave={(itemId, value) => saveOptimalBatch(itemId, value)}
              onOrderQuantitySave={(productId, value) => saveOrderQuantity(productId, value)}
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
      {produceOpen && activeRow && (
        <ProduceDialog
          produceRow={activeRow}
          produceError={produceError}
          // Закрытие цепочки возобновляемо: обе стороны могут быть уже
          // произведены, а комбинированный наряд — ещё нет. Что закрывать,
          // решает бэкенд по остатку каждой стороны.
          canProduceRow={activeRow.paint_weld_chain ? true : (activeRow.remaining_qty ?? 0) > 0}
          produceQty={produceQty}
          setProduceQty={setProduceQty}
          produceSaving={produceSaving}
          produceOverageQty={Math.max(0, Number(produceQty) - (activeRow.remaining_qty ?? 0))}
          produceOperations={produceOperations}
          produceChainSides={produceChainSides}
          produceOperationEmployees={produceOperationEmployees}
          setProduceOperationEmployees={setProduceOperationEmployees}
          employees={employees}
          employeesLoading={employeesLoading}
          produceOperationsLoading={produceOperationsLoading}
          produceEmployeeRef={produceEmployeeRef}
          setProduceEmployeeRef={setProduceEmployeeRef}
          allOperationExecutorsSelected={produceOperations.length > 0 && produceOperations.every(
            (operation) => Boolean(produceOperationEmployees[operation.spec_operation_id]),
          )}
          setProduceOpen={setProduceOpen}
          submitProduce={submitProduce}
        />
      )}
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
