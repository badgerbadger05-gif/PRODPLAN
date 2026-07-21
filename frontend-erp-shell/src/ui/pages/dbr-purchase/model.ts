import type { DbrPurchasePlanRow } from '../../../domain/dbr'

export type DbrPurchaseSortKey = 'order_before' | 'to_order_qty' | 'item_code'

export type DbrPurchaseSource =
  | { kind: 'active' }
  | { kind: 'program'; programId: number }

export type DbrPurchaseSourceParams =
  | { active: true; thresholdDays: number }
  | { programId: number; thresholdDays: number }

export function purchaseSourceFromKey(sourceKey: string): DbrPurchaseSource {
  return sourceKey === 'active'
    ? { kind: 'active' }
    : { kind: 'program', programId: Number(sourceKey) }
}

export function purchaseSourceParams(
  source: DbrPurchaseSource,
  thresholdDays: number,
): DbrPurchaseSourceParams {
  return source.kind === 'active'
    ? { active: true, thresholdDays }
    : { programId: source.programId, thresholdDays }
}

export function selectPurchaseRows(
  rows: readonly DbrPurchasePlanRow[],
  onlyToOrder: boolean,
  sort: DbrPurchaseSortKey,
): DbrPurchasePlanRow[] {
  const selected = rows.filter((row) => !onlyToOrder || row.to_order_qty > 0)
  selected.sort((a, b) => {
    switch (sort) {
      case 'to_order_qty':
        return b.to_order_qty - a.to_order_qty
      case 'item_code':
        return a.item_code.localeCompare(b.item_code)
      case 'order_before':
      default:
        return (a.order_before || '9999-12-31').localeCompare(b.order_before || '9999-12-31')
    }
  })
  return selected
}

export function countPurchaseRowsWithinHorizon(rows: readonly DbrPurchasePlanRow[]): number {
  return rows.filter((row) => row.to_order_qty > 0 && row.within_lead_time_threshold).length
}

export function purchaseSortableClass(active: DbrPurchaseSortKey, key: DbrPurchaseSortKey): string {
  return `dbrSortable ${active === key ? 'active' : ''}`
}

export function purchaseRowClass(row: DbrPurchasePlanRow): string {
  return row.to_order_qty > 0 && row.within_lead_time_threshold ? 'dbrPurchaseUrgent' : ''
}
