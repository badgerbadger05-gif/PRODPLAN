import { describe, expect, it } from 'vitest'
import type { DbrPurchasePlanRow } from '../../../domain/dbr'
import {
  countPurchaseRowsWithinHorizon,
  purchaseRowClass,
  purchaseSortableClass,
  purchaseSourceFromKey,
  purchaseSourceParams,
  selectPurchaseRows,
} from './model'

const rows: DbrPurchasePlanRow[] = [
  {
    item_id: 2,
    item_code: 'B-ITEM',
    item_name: 'B',
    demand_qty: 8,
    stock_qty: 0,
    open_order_qty: 0,
    available_qty: 0,
    to_order_qty: 8,
    replenishment_time: 5,
    order_before: null,
    within_lead_time_threshold: false,
  },
  {
    item_id: 1,
    item_code: 'A-ITEM',
    item_name: 'A',
    demand_qty: 4,
    stock_qty: 0,
    open_order_qty: 0,
    available_qty: 0,
    to_order_qty: 4,
    replenishment_time: 10,
    order_before: '2026-07-20',
    within_lead_time_threshold: true,
  },
  {
    item_id: 3,
    item_code: 'C-ITEM',
    item_name: 'C',
    demand_qty: 1,
    stock_qty: 5,
    open_order_qty: 0,
    available_qty: 5,
    to_order_qty: 0,
    replenishment_time: 3,
    order_before: '2026-07-10',
    within_lead_time_threshold: true,
  },
]

describe('DBR purchase model', () => {
  it('maps active and program keys to exact request parameters', () => {
    const active = purchaseSourceFromKey('active')
    const program = purchaseSourceFromKey('17')

    expect(active).toEqual({ kind: 'active' })
    expect(program).toEqual({ kind: 'program', programId: 17 })
    expect(purchaseSourceParams(active, 60)).toEqual({ active: true, thresholdDays: 60 })
    expect(purchaseSourceParams(program, 45)).toEqual({ programId: 17, thresholdDays: 45 })
  })

  it('filters rows to positive order quantities without mutating the source', () => {
    const originalOrder = rows.map((row) => row.item_id)

    expect(selectPurchaseRows(rows, true, 'order_before').map((row) => row.item_id)).toEqual([1, 2])
    expect(rows.map((row) => row.item_id)).toEqual(originalOrder)
  })

  it('sorts by item code, descending order quantity, or order deadline with empty dates last', () => {
    expect(selectPurchaseRows(rows, false, 'item_code').map((row) => row.item_code)).toEqual([
      'A-ITEM', 'B-ITEM', 'C-ITEM',
    ])
    expect(selectPurchaseRows(rows, false, 'to_order_qty').map((row) => row.to_order_qty)).toEqual([8, 4, 0])
    expect(selectPurchaseRows(rows, false, 'order_before').map((row) => row.item_id)).toEqual([3, 1, 2])
  })

  it('derives horizon, urgent-row, and active-sort presentation state', () => {
    expect(countPurchaseRowsWithinHorizon(rows)).toBe(1)
    expect(purchaseRowClass(rows[0])).toBe('')
    expect(purchaseRowClass(rows[1])).toBe('dbrPurchaseUrgent')
    expect(purchaseRowClass(rows[2])).toBe('')
    expect(purchaseSortableClass('item_code', 'item_code')).toBe('dbrSortable active')
    expect(purchaseSortableClass('item_code', 'order_before')).toBe('dbrSortable ')
  })
})
