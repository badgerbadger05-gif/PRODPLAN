import { describe, expect, it } from 'vitest'
import type { OrderRow, ProductionFilters, ProductionOperationOption, WorkshopWarehouse } from '../../../domain/productionControl'
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
} from './model'

const filters: ProductionFilters = {
  search: 'насос', status: 'ready', workshop_id: '', coverage_status: '', root_item_id: '', planning_contour: '',
  sort_by: 'planned_start_date', sort_dir: 'asc',
}
const rows = [
  { product_id: 1, item_id: 11, order_number: 'LOCAL', status: 'ready', coverage_status: 'ready' },
  { product_id: 2, item_id: 12, order_number: 'ERP', order_ref1c: 'REF', status: 'ready' },
] as OrderRow[]

describe('production control model', () => {
  it('builds list params from paging, focus, and non-empty filters', () => {
    const params = buildProductionOrderParams({
      filters, offset: 100, limit: 100, focusProductId: '9', focusOrderId: null,
    })
    expect(params.toString()).toBe('limit=100&offset=100&product_id=9&search=%D0%BD%D0%B0%D1%81%D0%BE%D1%81&status=ready&sort_by=planned_start_date&sort_dir=asc')
  })

  it('toggles the active sort direction without mutating filters', () => {
    expect(nextProductionSort(filters, 'planned_start_date')).toEqual({ ...filters, sort_dir: 'desc' })
    expect(filters.sort_dir).toBe('asc')
  })

  it('derives active, selected, specific, and deletable rows', () => {
    expect(activeProductionRow(rows, 2)?.product_id).toBe(2)
    expect(activeProductionRow(rows, 99)?.product_id).toBe(1)
    expect(productionRow(rows, 2)?.order_number).toBe('ERP')
    expect(selectedProductionRows(rows, new Set([1, 2]))).toHaveLength(2)
    expect(deletableProductionRows(rows, new Set([1, 2])).map((row) => row.product_id)).toEqual([1])
  })

  it('updates only coverage-driven rows and preserves posted issues', () => {
    const source = [
      rows[0],
      { ...rows[1], issue_status: 'posted', coverage_status: 'ready' },
    ]
    const updated = applyMaterialCoverage(source, 1, 'shortage', 'Дефицит')
    expect(updated[0]).toEqual(expect.objectContaining({ status: 'shortage', coverage_status: 'shortage', coverage_label: 'Дефицит' }))
    expect(updated[1]).toBe(source[1])
  })

  it('normalizes settings rows into the API payload', () => {
    const workshopRows = [{ workshop_id: 3, warehouse_ref1c: 'W3' }] as WorkshopWarehouse[]
    expect(buildProductionSettingsPayload(workshopRows, new Set(['IGN']))).toEqual({
      workshop_warehouses: [{ resource_id: 3, workshop_id: 3, warehouse_ref1c: 'W3', production_warehouse_ref1c: '' }],
      ignored_warehouses: [{ warehouse_ref1c: 'IGN' }],
    })
  })

  it('builds executor payloads and detects missing assignments', () => {
    const operations = [{ line_number: 1, spec_operation_id: 7, operation_id: 70 }] as ProductionOperationOption[]
    expect(areAllOperationExecutorsSelected(operations, {})).toBe(false)
    expect(areAllOperationExecutorsSelected(operations, { 7: 'E-1' })).toBe(true)
    expect(buildOperationExecutors(operations, { 7: 'E-1' }, [{ employee_id: 1, employee_ref1c: 'E-1', employee_name: 'Иванов' }])).toEqual([
      { line_number: 1, spec_operation_id: 7, operation_id: 70, employee_ref1c: 'E-1' },
    ])
  })

  it('formats page bounds for empty, partial, and full pages', () => {
    expect(productionPagination(0, 0, 0)).toEqual({ visibleFrom: 0, visibleTo: 0 })
    expect(productionPagination(100, 20, 125)).toEqual({ visibleFrom: 101, visibleTo: 120 })
  })
})
