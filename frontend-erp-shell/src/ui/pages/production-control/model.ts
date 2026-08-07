import {
  type EmployeeOption,
  type OrderRow,
  type ProductionFilters,
  type ProductionOperationOption,
  type WorkshopWarehouse,
} from '../../../domain/productionControl'
import type { ProductionOrderSortKey } from './productionOrdersDoctype'

export const DEFAULT_PRODUCTION_FILTERS: ProductionFilters = {
  search: '',
  status: '',
  workshop_id: '',
  coverage_status: '',
  root_item_id: '',
  planning_contour: '',
  sort_by: 'planned_start_date',
  sort_dir: 'asc',
}

export type ProductionControlUrlState = {
  filters: ProductionFilters
  offset: number
  activeProductId: number | null
}

const URL_FILTER_KEYS = [
  'search',
  'status',
  'workshop_id',
  'coverage_status',
  'root_item_id',
  'planning_contour',
  'sort_by',
  'sort_dir',
] as const

function nonNegativeInteger(value: string | null): number {
  if (!value || !/^\d+$/u.test(value)) return 0
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) ? parsed : 0
}

function positiveInteger(value: string | null): number | null {
  const parsed = nonNegativeInteger(value)
  return parsed > 0 ? parsed : null
}

export function parseProductionControlUrlState(
  params: URLSearchParams,
): ProductionControlUrlState {
  return {
    filters: {
      search: params.get('search') ?? '',
      status: params.get('status') ?? '',
      workshop_id: params.get('workshop_id') ?? '',
      coverage_status: params.get('coverage_status') ?? '',
      root_item_id: params.get('root_item_id') ?? '',
      planning_contour: params.get('planning_contour') === 'mrp' ? 'mrp' : '',
      sort_by: 'planned_start_date',
      sort_dir: params.get('sort_dir') === 'desc' ? 'desc' : 'asc',
    },
    offset: nonNegativeInteger(params.get('offset')),
    activeProductId: positiveInteger(params.get('active_product_id')),
  }
}

export function writeProductionControlUrlState(
  current: URLSearchParams,
  state: ProductionControlUrlState,
): URLSearchParams {
  const next = new URLSearchParams(current)
  for (const key of URL_FILTER_KEYS) next.delete(key)
  next.delete('offset')
  next.delete('active_product_id')

  for (const [key, value] of Object.entries(state.filters)) {
    if (!value) continue
    if (key === 'sort_by' && value === DEFAULT_PRODUCTION_FILTERS.sort_by) continue
    if (key === 'sort_dir' && value === DEFAULT_PRODUCTION_FILTERS.sort_dir) continue
    next.set(key, value)
  }
  if (state.offset > 0) next.set('offset', String(state.offset))
  if (state.activeProductId != null && state.activeProductId > 0) {
    next.set('active_product_id', String(state.activeProductId))
  }
  return next
}

export function buildProductionOrderParams({
  filters,
  offset,
  limit,
  focusProductId,
  focusOrderId,
}: {
  filters: ProductionFilters
  offset: number
  limit: number
  focusProductId?: string | null
  focusOrderId?: string | null
}): URLSearchParams {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (focusProductId) params.set('product_id', focusProductId)
  if (focusOrderId) params.set('order_id', focusOrderId)
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value)
  })
  return params
}

export function nextProductionSort(
  filters: ProductionFilters,
  key: ProductionOrderSortKey,
): ProductionFilters {
  return {
    ...filters,
    sort_by: key,
    sort_dir: filters.sort_by === key && filters.sort_dir === 'asc' ? 'desc' : 'asc',
  }
}

export function activeProductionRow(
  rows: readonly OrderRow[],
  activeId: number | null,
): OrderRow | null {
  return rows.find((row) => productionRowId(row) === activeId) ?? rows[0] ?? null
}

export function productionRowId(row: OrderRow): number {
  if (row.product_id != null) return row.product_id
  if (row.work_item_id != null) return -row.work_item_id
  return 0
}

export function selectedProductionRows(
  rows: readonly OrderRow[],
  selectedIds: ReadonlySet<number>,
): OrderRow[] {
  return rows.filter((row) => selectedIds.has(productionRowId(row)))
}

export function productionRow(rows: readonly OrderRow[], productId: number | null): OrderRow | null {
  return rows.find((row) => productionRowId(row) === productId) ?? null
}

export function deletableProductionRows(
  rows: readonly OrderRow[],
  selectedIds: ReadonlySet<number>,
): OrderRow[] {
  return selectedProductionRows(rows, selectedIds).filter(
    (row) => row.product_id != null && row.order_id != null && !row.order_ref1c,
  )
}

export function buildProductionSettingsPayload(
  workshopRows: readonly WorkshopWarehouse[],
  ignoredRefs: ReadonlySet<string>,
) {
  return {
    workshop_warehouses: workshopRows
      .map((row) => ({
        resource_id: row.resource_id ?? row.workshop_id,
        workshop_id: row.workshop_id ?? row.resource_id,
        warehouse_ref1c: row.warehouse_ref1c,
        production_warehouse_ref1c: row.production_warehouse_ref1c ?? '',
      }))
      .filter((row) => row.resource_id && row.warehouse_ref1c),
    ignored_warehouses: Array.from(ignoredRefs).map((warehouse_ref1c) => ({ warehouse_ref1c })),
  }
}

export function buildOperationExecutors(
  operations: readonly ProductionOperationOption[],
  employeeRefs: Readonly<Record<number, string>>,
  employees: readonly EmployeeOption[] = [],
) {
  return operations.map((operation) => {
    const employeeRef = employeeRefs[operation.spec_operation_id] || ''
    const employee = employees.find((row) => row.employee_ref1c === employeeRef)
    return {
      line_number: operation.line_number,
      spec_operation_id: operation.spec_operation_id,
      operation_id: operation.operation_id,
      employee_ref1c: employee?.employee_ref1c || employeeRef,
    }
  })
}

export function areAllOperationExecutorsSelected(
  operations: readonly ProductionOperationOption[],
  employeeRefs: Readonly<Record<number, string>>,
): boolean {
  return operations.length === 0
    || operations.every((operation) => Boolean(employeeRefs[operation.spec_operation_id]))
}

export function productionPagination(offset: number, rowCount: number, total: number) {
  return {
    visibleFrom: total ? offset + 1 : 0,
    visibleTo: Math.min(offset + rowCount, total),
  }
}
