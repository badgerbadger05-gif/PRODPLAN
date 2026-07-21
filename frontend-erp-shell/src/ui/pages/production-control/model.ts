import {
  coverageLabels,
  type EmployeeOption,
  type OrderRow,
  type ProductionFilters,
  type ProductionOperationOption,
  type WorkshopWarehouse,
} from '../../../domain/productionControl'
import type { ProductionOrderSortKey } from './productionOrdersDoctype'
import { coverageDrivenStatuses } from './helpers'

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
  return rows.find((row) => row.product_id === activeId) ?? rows[0] ?? null
}

export function selectedProductionRows(
  rows: readonly OrderRow[],
  selectedIds: ReadonlySet<number>,
): OrderRow[] {
  return rows.filter((row) => selectedIds.has(row.product_id))
}

export function productionRow(rows: readonly OrderRow[], productId: number | null): OrderRow | null {
  return rows.find((row) => row.product_id === productId) ?? null
}

export function deletableProductionRows(
  rows: readonly OrderRow[],
  selectedIds: ReadonlySet<number>,
): OrderRow[] {
  return selectedProductionRows(rows, selectedIds).filter((row) => !row.order_ref1c)
}

export function applyMaterialCoverage(
  rows: readonly OrderRow[],
  productId: number,
  coverageStatus: string,
  coverageLabel?: string | null,
): OrderRow[] {
  return rows.map((row) => {
    if (row.product_id !== productId) return row
    const canApply = (!row.issue_status || row.issue_status === 'not_requested')
      && coverageDrivenStatuses.has(String(row.coverage_status || row.status || ''))
    if (!canApply) return row
    return {
      ...row,
      status: coverageDrivenStatuses.has(String(row.status || '')) ? coverageStatus : row.status,
      coverage_status: coverageStatus,
      coverage_label: coverageLabel || coverageLabels[coverageStatus] || coverageStatus,
    }
  })
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
