import type { MrpProductionRow, MrpPurchaseRow } from '../../../domain/planning'

export type MrpResultTab = 'production' | 'purchases' | 'rework' | 'capacity'
export const PURCHASE_FILTER_MISSING_SUPPLIER = '__missing_supplier_name'
export const PURCHASE_FILTER_MISSING_CATEGORY = '__missing_category'

export function parseMrpResultTab(value: string | null): MrpResultTab | null {
  if (value === 'production' || value === 'purchases' || value === 'rework' || value === 'capacity') return value
  return null
}

export function parsePositiveId(value: string | null) {
  const id = Number(value)
  return Number.isFinite(id) && id > 0 ? id : null
}

export function supplierDisplayName(row: MrpPurchaseRow) {
  return (row.supplier_name || '').trim() || 'Без наименования'
}

export function supplierFilterKey(row: MrpPurchaseRow) {
  return (row.supplier_name || '').trim()
    ? (row.supplier_ref1c || row.supplier_name || '')
    : PURCHASE_FILTER_MISSING_SUPPLIER
}

export function categoryFilterKey(row: MrpPurchaseRow) {
  return row.category_id !== null && row.category_id !== undefined
    ? String(row.category_id)
    : (row.category_ref1c || PURCHASE_FILTER_MISSING_CATEGORY)
}

export function purchaseFilterOptions(rows: MrpPurchaseRow[]) {
  const suppliers = new Map<string, string>()
  const categories = new Map<string, string>()
  rows.forEach((row) => {
    const supplierKey = supplierFilterKey(row)
    if (supplierKey) suppliers.set(supplierKey, supplierDisplayName(row))
    const categoryKey = categoryFilterKey(row)
    if (categoryKey) categories.set(categoryKey, row.category_name || 'Без товарной группы')
  })
  const toOptions = (map: Map<string, string>) => (
    Array.from(map.entries())
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label, 'ru'))
  )
  return { suppliers: toOptions(suppliers), categories: toOptions(categories) }
}

export function buildPurchaseCategoryFilterParam(categoryFilter: string): { category_id?: number; category_ref1c?: string } {
  if (!categoryFilter) return {}
  if (/^\d+$/.test(categoryFilter)) {
    const categoryId = Number(categoryFilter)
    return Number.isFinite(categoryId) ? { category_id: categoryId } : {}
  }
  return { category_ref1c: categoryFilter }
}

export function toggleMany(set: Set<number>, ids: number[], checked: boolean) {
  const next = new Set(set)
  ids.forEach((id) => {
    if (checked) next.add(id)
    else next.delete(id)
  })
  return next
}

export function productionSourceIds(row: MrpProductionRow) {
  return row.source_order_ids?.length ? row.source_order_ids : [row.order_id]
}

export function isProductionRowSelectable(row: MrpProductionRow) {
  return Number(row.qty || 0) > 0
}

export function purchaseSourceIds(row: MrpPurchaseRow) {
  return row.source_purchase_ids?.length ? row.source_purchase_ids : [row.purchase_id]
}

export function formatActionResult(title: string, result: Record<string, unknown>) {
  const created = numberValue(result.created ?? result.created_count ?? result.orders_created ?? result.exported ?? result.exported_count)
  const existing = numberValue(result.existing ?? result.existing_count ?? result.orders_existing)
  const skipped = countValue(result.skipped ?? result.skipped_count ?? result.skipped_rows)
  const errors = countValue(result.errors ?? result.error_count)
  const parts = [`${title}: выполнено`]
  if (created) parts.push(`новых ${created}`)
  if (existing) parts.push(`уже было ${existing}`)
  if (skipped) parts.push(`пропущено ${skipped}`)
  if (errors) parts.push(`ошибок ${errors}`)
  if (parts.length > 1) return parts.join(', ')
  return `${title}: ${JSON.stringify(result).slice(0, 220)}`
}

function numberValue(value: unknown) {
  const number = Number(value ?? 0)
  return Number.isFinite(number) ? number : 0
}

function countValue(value: unknown) {
  if (Array.isArray(value)) return value.length
  return numberValue(value)
}
