import { describe, expect, it } from 'vitest'
import type { DbrPurchaseCockpitRow } from '../../../domain/dbr'
import { purchaseRowClass, purchaseSortableClass, selectPurchaseRows } from './model'

const rows: DbrPurchaseCockpitRow[] = [
  { item_id: 2, item_code: 'B-ITEM', item_name: 'B', warehouse_ref1c: 'W4', planning_stock_pool: 'pool', reservation_ids: [2], obligations: [{ reservation_id: 2, outstanding_qty: 8, uncovered_qty: 8, coverage: [] }], outstanding_obligation_qty: 8, uncovered_qty: 8, to_order_qty: 8, stock_qty: 0, exact_future_supply_qty: 0, need_date: null },
  { item_id: 1, item_code: 'A-ITEM', item_name: 'A', warehouse_ref1c: 'W4', planning_stock_pool: 'pool', reservation_ids: [1], obligations: [{ reservation_id: 1, outstanding_qty: 4, uncovered_qty: 4, coverage: [] }], outstanding_obligation_qty: 4, uncovered_qty: 4, to_order_qty: 4, stock_qty: 0, exact_future_supply_qty: 0, need_date: '2026-07-20' },
  { item_id: 3, item_code: 'C-ITEM', item_name: 'C', warehouse_ref1c: 'W4', planning_stock_pool: 'pool', reservation_ids: [3], obligations: [{ reservation_id: 3, outstanding_qty: 1, uncovered_qty: 0, coverage: [] }], outstanding_obligation_qty: 1, uncovered_qty: 0, to_order_qty: 0, stock_qty: 5, exact_future_supply_qty: 0, need_date: '2026-07-10' },
]

describe('DBR purchase saved-cockpit model', () => {
  it('filters open Ledger obligations without mutating captured rows', () => {
    const originalOrder = rows.map((row) => row.item_id)
    expect(selectPurchaseRows(rows, true, 'need_date').map((row) => row.item_id)).toEqual([1, 2])
    expect(rows.map((row) => row.item_id)).toEqual(originalOrder)
  })

  it('sorts only Ledger-native row axes', () => {
    expect(selectPurchaseRows(rows, false, 'item_code').map((row) => row.item_code)).toEqual(['A-ITEM', 'B-ITEM', 'C-ITEM'])
    expect(selectPurchaseRows(rows, false, 'to_order_qty').map((row) => row.to_order_qty)).toEqual([8, 4, 0])
    expect(selectPurchaseRows(rows, false, 'need_date').map((row) => row.item_id)).toEqual([3, 1, 2])
  })

  it('keeps no fabricated urgency state and exposes sort state', () => {
    expect(purchaseRowClass(rows[0])).toBe('')
    expect(purchaseRowClass(rows[1])).toBe('')
    expect(purchaseSortableClass('item_code', 'item_code')).toBe('dbrSortable active')
    expect(purchaseSortableClass('item_code', 'need_date')).toBe('dbrSortable ')
  })
})
