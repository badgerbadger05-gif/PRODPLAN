import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  coverageLabels,
  type ControlSettings,
  type ControlWarehouse,
  type EmployeeOption,
  type EmployeesResponse,
  type MaterialIssueCreateResponse,
  type MaterialsResponse,
  type OrderRow,
  type OrdersResponse,
  type ProductionOperationOption,
  type ProductionOperationsResponse,
  type ProductionFilters,
  type WarehouseCandidate,
  type WorkshopWarehouse,
} from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'
import { api } from '../../lib/api'
import { getPeriodPlanMatrix } from '../../services/periodPlan'
import { listResources } from '../../services/resources'
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
  const [sourcePlanId, setSourcePlanId] = useState<number | null>(null)
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
  const produceRow = useMemo(() => rows.find((row) => row.product_id === produceProductId) ?? null, [rows, produceProductId])
  const selectedEmployee = useMemo(
    () => employees.find((employee) => employee.employee_ref1c === produceEmployeeRef) ?? null,
    [employees, produceEmployeeRef],
  )
  const allOperationExecutorsSelected = useMemo(
    () => produceOperations.length === 0 || produceOperations.every((operation) => Boolean(produceOperationEmployees[operation.spec_operation_id])),
    [produceOperations, produceOperationEmployees],
  )
  const produceRemainingQty = Number(produceRow?.remaining_qty ?? 0)
  const produceRequestedQty = Number(produceQty || 0)
  const produceOverageQty = produceRequestedQty - produceRemainingQty
  const canProduceRow = Boolean(produceRow && produceRemainingQty > 0)

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const params = new URLSearchParams()
      params.set('limit', String(limit))
      params.set('offset', String(nextOffset))
      if (focusProductId) params.set('product_id', focusProductId)
      if (focusOrderId) params.set('order_id', focusOrderId)
      Object.entries(filtersRef.current).forEach(([key, value]) => {
        if (value) params.set(key, value)
      })
      const data = await api<OrdersResponse>(`/v1/production-control/orders?${params.toString()}`)
      setRows(data.rows ?? [])
      setTotal(data.total ?? 0)
      setRunId(data.latest_run_id ?? null)
      setSourcePlanId(data.latest_source_plan_id ?? null)
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
      setResources(await listResources())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

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
    if (sourcePlanId) void loadRootOptions(sourcePlanId)
    else setRootOptions([])
    return () => { cancelled = true }
  }, [sourcePlanId])

  const loadEmployees = useCallback(async () => {
    setEmployeesLoading(true)
    try {
      const data = await api<EmployeesResponse>('/v1/production-control/employees')
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
      const data = await api<ProductionOperationsResponse>(`/v1/production-control/orders/${productId}/operations`)
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
          workshop_warehouses: workshopRows
            .map((row) => ({
              resource_id: row.resource_id ?? row.workshop_id,
              workshop_id: row.workshop_id ?? row.resource_id,
              warehouse_ref1c: row.warehouse_ref1c,
              production_warehouse_ref1c: row.production_warehouse_ref1c ?? '',
            }))
            .filter((row) => row.resource_id && row.warehouse_ref1c),
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

  const loadMaterials = useCallback(async (productId: number, refresh = true) => {
    setActiveId(productId)
    setMaterials(null)
    try {
      const data = await api<MaterialsResponse>(`/v1/production-control/orders/${productId}/materials${refresh ? '?refresh=true' : ''}`)
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
      await api(`/v1/production-control/orders/${row.product_id}/state`, {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      })
    } catch (e) {
      setRows((list) => list.map((item) => item.product_id === row.product_id ? { ...item, status: previous } : item))
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function requestMaterialIssues(sourceWarehouseRef: string | undefined, productIds: number[]) {
    const body: Record<string, unknown> = { product_ids: productIds, initiated_by: 'erp-shell' }
    if (sourceWarehouseRef) body.source_warehouse_ref1c = sourceWarehouseRef
    return api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
      method: 'POST',
      body: JSON.stringify(body),
    })
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
    void fetch('/api/v1/production-control/route-sheets/print', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_ids: ids, mark_printed: true, auto_print: true }),
    })
      .then(async (response) => {
        const html = await response.text()
        if (!response.ok) throw new Error(html || response.statusText)
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
      const result = await api<Record<string, unknown>>('/v1/production-control/material-issues/export-to-1c', {
        method: 'POST',
        body: JSON.stringify({ issue_ids: issueIds, dry_run: false, allow_production: true }),
      })
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
      const result = await api<Record<string, unknown>>('/v1/production-control/sync-posted-transfers', {
        method: 'POST',
      })
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
        await api(`/v1/production-control/orders/${produceRow.product_id}/quantity`, {
          method: 'PATCH',
          body: JSON.stringify({ quantity: targetQuantity }),
        })
        const issueResult = await api<MaterialIssueCreateResponse>('/v1/production-control/material-issues', {
          method: 'POST',
          body: JSON.stringify({ product_ids: [produceRow.product_id], initiated_by: 'erp-shell-overproduction' }),
        })
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
          const transferResult = await api<Record<string, unknown>>('/v1/production-control/material-issues/export-to-1c', {
            method: 'POST',
            body: JSON.stringify({ issue_ids: issueIds, dry_run: false, allow_production: true }),
          })
          const transferErrors = Number(transferResult.issues_error ?? 0)
          const transferCreated = Number(transferResult.issues_created ?? 0)
          const transferExisting = Number(transferResult.issues_already_linked ?? 0)
          if (transferErrors > 0 || transferCreated + transferExisting < 1 || transferResult.status === 'partial_error') {
            const detail = firstExportProblem(transferResult, transferResult.parent_orders_export as Record<string, unknown> | undefined)
            throw new Error(`Дополнительное перемещение на разницу не выгружено в 1С.${detail ? ` ${detail}` : ''}`)
          }
          for (const issueId of issueIds) {
            await api(`/v1/production-control/material-issues/${issueId}/assembled`, {
              method: 'POST',
              body: JSON.stringify({ allow_production: true }),
            })
          }
        }
      }

      // Step 1: record manufacture locally (bumps produced_qty / remaining_qty).
      const operationExecutors = produceOperations.map((operation) => {
        const employeeRef = produceOperationEmployees[operation.spec_operation_id]
        const employee = employees.find((row) => row.employee_ref1c === employeeRef)
        return {
          line_number: operation.line_number,
          spec_operation_id: operation.spec_operation_id,
          operation_id: operation.operation_id,
          employee_ref1c: employee?.employee_ref1c || employeeRef,
        }
      })
      const localResult = await api<Record<string, unknown>>(
        `/v1/production-control/orders/${produceRow.product_id}/produce`,
        {
          method: 'POST',
          body: JSON.stringify({
            qty: requestedQty,
            executor: produceOperations.length ? undefined : selectedEmployee?.employee_name || undefined,
            operation_executors: produceOperations.length ? operationExecutors : undefined,
          }),
        },
      )
      const manufacture_id = Number(localResult.manufacture_id)
      manufactureIdToRollback = manufacture_id

      if (produceDryRun) {
        const dryRunResult = await api<Record<string, unknown>>(
          '/v1/production-control/manufactures/export-to-1c',
          {
            method: 'POST',
            body: JSON.stringify({ manufacture_ids: [manufacture_id], dry_run: true, allow_production: false }),
          },
        )
        await api(`/v1/production-control/manufactures/${manufacture_id}/rollback-local`, { method: 'POST' })
        setProduceDryRunPayload(JSON.stringify(dryRunResult, null, 2))
        await load(offsetRef.current)
        return
      }

      // Step 2: export the manufacture to 1C as Document_СборкаЗапасов (Posted=false).
      const exportResult = await api<Record<string, unknown>>(
        '/v1/production-control/manufactures/export-to-1c',
        {
          method: 'POST',
          body: JSON.stringify({ manufacture_ids: [manufacture_id], dry_run: false, allow_production: true }),
        },
      )
      const created1c = Number(exportResult.manufactures_created ?? 0)
      const errored = Number(exportResult.manufactures_error ?? 0)
      const exportEntry = recordArray(exportResult.entries)[0]
      const ref = exportEntry?.target_ref_key
      manufactureExportedRef = typeof ref === 'string' ? ref : ''
      if (errored > 0 || created1c < 1 || !ref) {
        const exportError = exportEntry?.error || exportEntry?.reason || firstExportProblem(exportResult)
        if (!ref) {
          await api(`/v1/production-control/manufactures/${manufacture_id}/rollback-local`, { method: 'POST' })
          throw new Error(String(exportError || '1C не создала документ выпуска; локальный выпуск откатан'))
        }
        throw new Error(
          `1C создала документ выпуска ${String(ref).slice(0, 8)}…, но не провела его: ` +
          `${String(exportError || 'ошибка проведения')}. Локальный выпуск оставлен для разбора.`,
        )
      }
      const pieceworkResult = await api<Record<string, unknown>>(
        '/v1/production-control/manufactures/export-piecework-to-1c',
        {
          method: 'POST',
          body: JSON.stringify({ manufacture_ids: [manufacture_id], dry_run: false, allow_production: true }),
        },
      )
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
          await api(`/v1/production-control/manufactures/${manufactureIdToRollback}/rollback-local`, { method: 'POST' })
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
      setProduceSaving(false)
    }
  }

  async function fillRemaining(_runId: number, requirementId: number) {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await api<Record<string, unknown>>('/v1/production-control/orders/from-mrp-requirements', {
        method: 'POST',
        body: JSON.stringify({ requirement_ids: [requirementId], initiated_by: 'erp-shell' }),
      })
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
    const result = await api<{
      quantity: number
      remaining_qty: number
      mrp_req_net_qty?: number | null
      mrp_req_covered_qty?: number | null
      mrp_req_remaining_qty?: number | null
    }>(`/v1/production-control/orders/${productId}/quantity`, {
      method: 'PATCH',
      body: JSON.stringify({ quantity: value }),
    })
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
        await api(`/v1/production-control/orders/${row.product_id}`, { method: 'DELETE' })
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
          onProduce={() => openProduceDialog()}
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
              onPrint={() => activeRow && printRows([activeRow.product_id])}
              onOptimalBatchSave={(itemId, value) => saveOptimalBatch(itemId, value)}
              onQuantitySave={(productId, value) => saveOrderQuantity(productId, value)}
              onFillRemaining={(sourceRunId, requirementId) => fillRemaining(sourceRunId, requirementId)}
            />
          )}
        </div>
      </DocumentWindow>

      {produceOpen && produceRow && (
        <div className="dialogOverlay" onClick={(e) => { if (e.target === e.currentTarget) setProduceOpen(false) }}>
          <div className="dialogBox">
            <div className="dialogHeader">Произвести - {produceRow.item_name}</div>
            <div className="dialogBody">
              {produceError && <div className="dialogError">{produceError}</div>}
              {!canProduceRow && (
                <div className="fieldHint danger">Эта строка уже произведена полностью.</div>
              )}
              <div className="dialogField">
                <label>Количество ({produceRow.unit || 'шт'})</label>
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={produceQty}
                  onChange={(e) => setProduceQty(e.target.value)}
                  disabled={produceSaving}
                />
                {produceOverageQty > 0.000001 && (
                  <div className="fieldHint">
                    Больше плана на {produceOverageQty.toLocaleString('ru-RU')}: будет создано дополнительное перемещение материалов.
                  </div>
                )}
              </div>
              {produceOperations.length > 0 ? (
                <div className="dialogField">
                  <label>Исполнители операций</label>
                  <div className="operationExecutorList">
                    {produceOperations.map((operation) => (
                      <div className="operationExecutorRow" key={operation.spec_operation_id}>
                        <div className="operationExecutorMeta">
                          <strong>{operation.line_number}. {operation.operation_name || 'Операция'}</strong>
                          <span>{operation.stage_name || 'Этап не указан'} · норма {Number(operation.time_norm ?? 0).toLocaleString('ru-RU')}</span>
                        </div>
                        <select
                          value={produceOperationEmployees[operation.spec_operation_id] || ''}
                          onChange={(e) => setProduceOperationEmployees((current) => ({
                            ...current,
                            [operation.spec_operation_id]: e.target.value,
                          }))}
                          disabled={produceSaving || employeesLoading || produceOperationsLoading}
                        >
                          <option value="">{employeesLoading ? 'Загрузка сотрудников...' : 'Выберите сотрудника'}</option>
                          {employees.map((employee) => (
                            <option key={employee.employee_ref1c} value={employee.employee_ref1c}>
                              {employee.employee_name}{employee.employee_type === 'brigade' ? ' [бригада]' : ''}{employee.employee_code ? ` (${employee.employee_code})` : ''}
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                  {!employeesLoading && employees.length === 0 && (
                    <div className="fieldHint">Список пуст. Запустите синхронизацию сотрудников в разделе «Синхронизация».</div>
                  )}
                </div>
              ) : (
                <div className="dialogField">
                  <label>Исполнитель</label>
                  <select
                    value={produceEmployeeRef}
                    onChange={(e) => setProduceEmployeeRef(e.target.value)}
                    disabled={produceSaving || employeesLoading || produceOperationsLoading}
                  >
                    <option value="">{produceOperationsLoading || employeesLoading ? 'Загрузка...' : 'Выберите сотрудника'}</option>
                    {employees.map((employee) => (
                      <option key={employee.employee_ref1c} value={employee.employee_ref1c}>
                        {employee.employee_name}{employee.employee_type === 'brigade' ? ' [бригада]' : ''}{employee.employee_code ? ` (${employee.employee_code})` : ''}
                      </option>
                    ))}
                  </select>
                  {!employeesLoading && employees.length === 0 && (
                    <div className="fieldHint">Список пуст. Запустите синхронизацию сотрудников в разделе «Синхронизация».</div>
                  )}
                </div>
              )}
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
              <button
                className="primary"
                onClick={() => void submitProduce()}
                disabled={!canProduceRow || produceSaving || employeesLoading || produceOperationsLoading || (employees.length > 0 && (produceOperations.length ? !allOperationExecutorsSelected : !produceEmployeeRef))}
              >
                {produceSaving ? 'Создаём...' : produceDryRun ? 'Показать payload' : 'Создать в 1С'}
              </button>
            </div>
          </div>
        </div>
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
