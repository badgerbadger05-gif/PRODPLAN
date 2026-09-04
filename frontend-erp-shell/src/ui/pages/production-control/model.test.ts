import { describe, expect, it } from 'vitest'
import type { OrderRow, ProductionFilters, ProductionOperationOption, WorkshopWarehouse } from '../../../domain/productionControl'
import {
  activeProductionRow,
  areAllOperationExecutorsSelected,
  buildOperationExecutors,
  buildProductionOrderParams,
  buildProductionSettingsPayload,
  DEFAULT_PRODUCTION_FILTERS,
  deletableProductionRows,
  nextProductionSort,
  parseProductionControlUrlState,
  productionPagination,
  productionRow,
  productionRowId,
  productionRowProductIds,
  selectedProductionRows,
  writeProductionControlUrlState,
} from './model'

const filters: ProductionFilters = {
  search: 'насос', status: 'ready', workshop_id: '', coverage_status: '', root_item_id: '', planning_contour: '',
  launch_source: '', sort_by: 'planned_start_date', sort_dir: 'asc',
}
const rows = [
  { product_id: 1, order_id: 1, item_id: 11, order_number: 'LOCAL', status: 'ready', coverage_status: 'ready' },
  { product_id: 2, order_id: 2, item_id: 12, order_number: 'ERP', order_ref1c: 'REF', status: 'ready' },
] as OrderRow[]
const proposal = {
  journal_row_key: 'work-item:7', work_item_id: 7, product_id: null, order_id: null,
  item_id: 13, order_number: 'MRP-R-7', status: 'shortage', coverage_status: 'unknown',
} as OrderRow

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

  it('round-trips inspectable page URL state while preserving external focus', () => {
    const current = new URLSearchParams('product_id=9&order_id=4&tracking=keep')
    const next = writeProductionControlUrlState(current, {
      filters: {
        ...filters,
        workshop_id: '3',
        coverage_status: 'shortage',
        root_item_id: '44',
        planning_contour: 'mrp',
        sort_dir: 'desc',
      },
      view: 'mechshop',
      offset: 100,
      activeProductId: 2,
    })

    expect(next.get('product_id')).toBe('9')
    expect(next.get('order_id')).toBe('4')
    expect(next.get('tracking')).toBe('keep')
    expect(parseProductionControlUrlState(next)).toEqual({
      filters: {
        search: 'насос',
        status: 'ready',
        workshop_id: '3',
        coverage_status: 'shortage',
        root_item_id: '44',
        planning_contour: 'mrp',
        launch_source: 'drum_readiness',
        sort_by: 'readiness_priority_key',
        sort_dir: 'desc',
      },
      view: 'mechshop',
      offset: 100,
      activeProductId: 2,
    })
  })

  it('fails closed to page defaults for malformed URL values', () => {
    const parsed = parseProductionControlUrlState(new URLSearchParams(
      'offset=-1&active_product_id=0&planning_contour=legacy&sort_by=unknown&sort_dir=sideways&unknown=x',
    ))
    expect(parsed).toEqual({
      filters: DEFAULT_PRODUCTION_FILTERS,
      view: 'orders',
      offset: 0,
      activeProductId: null,
    })
  })

  it('derives active, selected, specific, and deletable rows', () => {
    expect(activeProductionRow(rows, 2)?.product_id).toBe(2)
    expect(activeProductionRow(rows, 99)?.product_id).toBe(1)
    expect(productionRow(rows, 2)?.order_number).toBe('ERP')
    expect(selectedProductionRows(rows, new Set([1, 2]))).toHaveLength(2)
    expect(deletableProductionRows(rows, new Set([1, 2])).map((row) => row.product_id)).toEqual([1])
    expect(productionRowId(proposal)).toBe(-7)
    expect(activeProductionRow([...rows, proposal], -7)).toBe(proposal)
    expect(selectedProductionRows([...rows, proposal], new Set([-7]))).toEqual([proposal])
    expect(deletableProductionRows([...rows, proposal], new Set([-7]))).toEqual([])
  })

  it('uses backend-provided chain membership for combined actions', () => {
    const painted = {
      ...rows[0],
      product_id: 11,
      order_number: 'PAINT',
      paint_weld_chain: { role: 'painted', link_id: 7, counterpart_product_id: 12 },
    } as OrderRow

    expect(productionRowProductIds(painted)).toEqual([11, 12])
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
    expect(buildOperationExecutors(operations, { 7: 'E-1' }, [{ employee_id: 1, employee_ref1c: 'E-1', employee_type: 'employee', employee_name: 'Иванов' }])).toEqual([
      { line_number: 1, spec_operation_id: 7, operation_id: 70, employee_ref1c: 'E-1' },
    ])
  })

  it('formats page bounds for empty, partial, and full pages', () => {
    expect(productionPagination(0, 0, 0)).toEqual({ visibleFrom: 0, visibleTo: 0 })
    expect(productionPagination(100, 20, 125)).toEqual({ visibleFrom: 101, visibleTo: 120 })
  })
})
