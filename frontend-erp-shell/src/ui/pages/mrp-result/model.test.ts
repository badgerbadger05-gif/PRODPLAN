import { describe, expect, it } from 'vitest'
import type { MrpProductionRow, MrpPurchaseRow } from '../../../domain/planning'
import {
  buildPurchaseCategoryFilterParam,
  PURCHASE_FILTER_MISSING_CATEGORY,
  PURCHASE_FILTER_MISSING_SUPPLIER,
  formatActionResult,
  isProductionRowSelectable,
  parseMrpResultTab,
  parsePositiveId,
  productionSourceIds,
  purchaseFilterOptions,
  purchaseSourceIds,
  toggleMany,
} from './model'

const production = (values: Partial<MrpProductionRow>): MrpProductionRow => values as MrpProductionRow
const purchase = (values: Partial<MrpPurchaseRow>): MrpPurchaseRow => values as MrpPurchaseRow

describe('MRP result model', () => {
  it('parses supported tabs and positive IDs only', () => {
    expect(parseMrpResultTab('purchases')).toBe('purchases')
    expect(parseMrpResultTab('unknown')).toBeNull()
    expect(parsePositiveId('42')).toBe(42)
    expect(parsePositiveId('0')).toBeNull()
    expect(parsePositiveId('abc')).toBeNull()
  })

  it('builds stable supplier and category options', () => {
    const rows = [
      purchase({ purchase_id: 1, supplier_name: ' Альфа ', supplier_ref1c: 'supplier-a', category_id: 7, category_name: 'Металл' }),
      purchase({ purchase_id: 2, supplier_name: '', supplier_ref1c: 'hidden-ref', category_ref1c: 'category-b', category_name: 'Крепёж' }),
      purchase({ purchase_id: 3, supplier_name: 'Альфа', supplier_ref1c: 'supplier-a', category_id: 7, category_name: 'Металл' }),
      purchase({ purchase_id: 4, supplier_name: ' ', supplier_ref1c: 'missing-name-ref' }),
    ]

    expect(purchaseFilterOptions(rows)).toEqual({
      suppliers: [
        { value: 'supplier-a', label: 'Альфа' },
        { value: PURCHASE_FILTER_MISSING_SUPPLIER, label: 'Без наименования' },
      ],
      categories: [
        { value: PURCHASE_FILTER_MISSING_CATEGORY, label: 'Без товарной группы' },
        { value: 'category-b', label: 'Крепёж' },
        { value: '7', label: 'Металл' },
      ],
    })
    expect(buildPurchaseCategoryFilterParam('11')).toEqual({ category_id: 11 })
    expect(buildPurchaseCategoryFilterParam('category-b')).toEqual({ category_ref1c: 'category-b' })
    expect(buildPurchaseCategoryFilterParam('')).toEqual({})
  })

  it('expands aggregate source IDs and detects selectable production rows', () => {
    expect(productionSourceIds(production({ order_id: 10, source_order_ids: [11, 12] }))).toEqual([11, 12])
    expect(productionSourceIds(production({ order_id: 10, source_order_ids: [] }))).toEqual([10])
    expect(purchaseSourceIds(purchase({ purchase_id: 20, source_purchase_ids: [21, 22] }))).toEqual([21, 22])
    expect(purchaseSourceIds(purchase({ purchase_id: 20 }))).toEqual([20])
    expect(isProductionRowSelectable(production({ qty: 1 }))).toBe(true)
    expect(isProductionRowSelectable(production({ qty: 0 }))).toBe(false)
  })

  it('toggles source IDs without mutating the prior selection', () => {
    const current = new Set([1, 3])
    const added = toggleMany(current, [2, 3], true)
    const removed = toggleMany(added, [1, 2], false)
    expect([...current]).toEqual([1, 3])
    expect([...added]).toEqual([1, 3, 2])
    expect([...removed]).toEqual([3])
  })

  it('formats action summaries including array counters and fallback payload', () => {
    expect(formatActionResult('Создание заказов', {
      created_count: 2,
      orders_existing: 1,
      skipped_rows: [{}, {}],
      errors: ['ошибка'],
    })).toBe('Создание заказов: выполнено, новых 2, уже было 1, пропущено 2, ошибок 1')
    expect(formatActionResult('Выгрузка', { status: 'ok' })).toBe('Выгрузка: {"status":"ok"}')
  })
})
