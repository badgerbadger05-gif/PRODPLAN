import type { components } from '../lib/apiTypes'

export type PurchaseLineStatus =
  | 'to_order'
  | 'overdue'
  | 'no_date'
  | 'expected'
  | 'partial'
  | 'received'
  | 'closed'
  | 'unavailable'

export type PurchaseFactStatus = 'available' | 'unavailable'

// Фаза движения товара по модели снабжения (группировка состояний 1С).
export type SupplyPhase = 'no_goods' | 'in_transit' | 'in_stock' | 'terminal' | 'unknown'

export type PurchaseCoverageSlice = {
  plan_period_from: string | null
  plan_period_to: string | null
  period_label: string | null
  required_qty: number
  realized_qty: number
  open_order_covered_qty: number
  to_order_qty: number
  to_order_pct: number
  coverage_slices: unknown[]
}

export type PurchaseHorizonBucket = {
  plan_period_to: string | null
  period_label: string
  item_count: number
  total_qty: number
}

export type PurchaseRow = {
  row_key: string
  line_id: number | null
  purchase_id: number | null
  source_purchase_ids?: number[]
  order_id: number | null
  order_number: string
  order_date: string | null
  order_ref1c: string | null
  order_state_name: string | null
  source: 'mrp' | '1c' | 'ledger'
  supplier_id: number | null
  supplier_name: string
  item_id: number
  item_code: string
  item_article: string | null
  item_name: string
  unit: string | null
  quantity: number
  received_qty: number | null
  remaining_qty: number
  delivery_date: string | null
  need_date: string | null
  overdue_days: number | null
  line_status: PurchaseLineStatus
  supply_phase: SupplyPhase
  counts_in_mrp: boolean | null
  price: number | null
  amount: number | null
  run_id: number | null
  run_ids?: number[]
  requirement_ids?: number[]
  reservation_ids?: number[]
  planning_stock_pool?: string | null
  required_qty?: number
  realized_qty?: number
  open_order_covered_qty?: number
  to_order_qty?: number
  to_order_pct?: number | null
  open_order_covered_pct?: number | null
  plan_period_from?: string | null
  plan_period_to?: string | null
  period_label?: string | null
  horizon_bucket_count?: number
  horizon_buckets?: PurchaseHorizonBucket[]
  slices?: PurchaseCoverageSlice[]
  row_generator?: string | null
  can_materialize: boolean
  materialize_disabled_reason?: string | null
  fact_status: PurchaseFactStatus
  fact_source: string
}

export function purchaseIdsForRow(row: Pick<PurchaseRow, 'purchase_id' | 'source_purchase_ids'>): number[] {
  const ids = [
    ...(row.purchase_id === null ? [] : [row.purchase_id]),
    ...(row.source_purchase_ids ?? []),
  ]
  return [...new Set(ids.filter((id) => Number.isInteger(id) && id > 0))]
}

export type PurchaseJournalSummary = {
  total_rows: number
  by_status: Record<string, number>
  by_phase: Record<string, number>
  to_order: number
  overdue: number
  expected_7d: number
  in_transit_amount: number
  fact_status: PurchaseFactStatus
}

export type PurchaseSelectionSummaryRequest = components['schemas']['PurchaseControlSelectionSummaryRequest']
export type PurchaseSelectionSummary = components['schemas']['PurchaseControlSelectionSummaryResponse']

export type PurchaseSnapshotMeta = {
  snapshot_id?: number
  ledger_generation: number
  ledger_generation_id?: number
  cutoff: string
  truth_status: string
  truth_reason?: string | null
  fact_source: string
  received_qty_status: PurchaseFactStatus
  read_only: boolean
}

export type PurchaseOrdersResponse = {
  rows: PurchaseRow[]
  total: number
  limit: number
  offset: number
  run_id: number | null
  run_ids: number[]
  truth_status: string
  ledger_generation_id: number
  summary: PurchaseJournalSummary
  meta: PurchaseSnapshotMeta
}

export type PurchaseOrderCard = {
  order: {
    order_id: number
    order_number: string
    order_date: string | null
    order_ref1c: string | null
    order_state_name: string | null
    supply_phase: SupplyPhase
    counts_in_mrp: boolean
    deletion_mark: boolean
    is_posted: boolean
    document_amount: number
    active: boolean
    source: 'mrp' | '1c'
    supplier_id: number | null
    supplier_name: string
  }
  lines: PurchaseRow[]
  meta: PurchaseSnapshotMeta
}

export type PurchaseSupplierOption = {
  supplier_id: number
  supplier_name: string
}

export type PurchaseFiltersResponse = {
  suppliers: PurchaseSupplierOption[]
  states: string[]
}

export type PurchaseFilters = {
  search: string
  supplier_id: string
  line_status: string
  state: string
  phase: string
  active_only: boolean
  include_to_order: boolean
  horizon_period_to: string
  sort_by: 'delivery_date' | 'order_date'
  sort_dir: 'asc' | 'desc'
}

export const purchaseLineStatusLabels: Record<PurchaseLineStatus, string> = {
  to_order: 'К заказу',
  overdue: 'Просрочен',
  no_date: 'Без даты',
  expected: 'Ожидается',
  partial: 'Частично',
  received: 'Поступил',
  closed: 'Закрыт',
  unavailable: 'Факт недоступен',
}

// Переиспользуем цветовые классы пилюль журнала производства
const purchaseLineStatusPillClasses: Record<PurchaseLineStatus, string> = {
  to_order: 'in_progress',
  overdue: 'shortage',
  no_date: 'partial',
  expected: 'to_move',
  partial: 'done',
  received: 'ready',
  closed: 'completed',
  unavailable: 'completed',
}

export function purchaseLineStatusLabel(status: string): string {
  return purchaseLineStatusLabels[status as PurchaseLineStatus] ?? status
}

export function purchaseLineStatusPillClass(status: string): string {
  return purchaseLineStatusPillClasses[status as PurchaseLineStatus] ?? 'completed'
}

export const purchaseLineStatusOptions = (Object.entries(purchaseLineStatusLabels) as Array<[PurchaseLineStatus, string]>)
  .filter(([value]) => value !== 'closed')

// Фазы движения товара: подписи и цветовые классы пилюль
export const supplyPhaseLabels: Record<SupplyPhase, string> = {
  no_goods: 'Нет товара',
  in_transit: 'Товар в пути',
  in_stock: 'На складе',
  terminal: 'Закрыт',
  unknown: 'Не определён',
}

const supplyPhasePillClasses: Record<SupplyPhase, string> = {
  no_goods: 'in_progress',
  in_transit: 'to_move',
  in_stock: 'ready',
  terminal: 'completed',
  unknown: 'completed',
}

export function supplyPhaseLabel(phase: string): string {
  return supplyPhaseLabels[phase as SupplyPhase] ?? phase
}

export function supplyPhasePillClass(phase: string): string {
  return supplyPhasePillClasses[phase as SupplyPhase] ?? 'completed'
}

// Фазы для сводных счётчиков/фильтра (без terminal/unknown)
export const supplyPhaseOptions: Array<[SupplyPhase, string]> = [
  ['no_goods', supplyPhaseLabels.no_goods],
  ['in_transit', supplyPhaseLabels.in_transit],
  ['in_stock', supplyPhaseLabels.in_stock],
]
