import type { DbrPurchaseCockpitRow } from '../../../domain/dbr'

export type DbrPurchaseSortKey = 'need_date' | 'to_order_qty' | 'item_code'

export function selectPurchaseRows(
  rows: readonly DbrPurchaseCockpitRow[],
  onlyToOrder: boolean,
  sort: DbrPurchaseSortKey,
): DbrPurchaseCockpitRow[] {
  const selected = rows.filter((row) => !onlyToOrder || row.to_order_qty > 0)
  selected.sort((a, b) => {
    switch (sort) {
      case 'to_order_qty':
        return b.to_order_qty - a.to_order_qty
      case 'item_code':
        return a.item_code.localeCompare(b.item_code)
      case 'need_date':
      default:
        return (a.need_date || '9999-12-31').localeCompare(b.need_date || '9999-12-31')
    }
  })
  return selected
}

export function purchaseSortableClass(active: DbrPurchaseSortKey, key: DbrPurchaseSortKey): string {
  return `dbrSortable ${active === key ? 'active' : ''}`
}

export function purchaseRowClass(row: DbrPurchaseCockpitRow): string {
  void row
  return ''
}
