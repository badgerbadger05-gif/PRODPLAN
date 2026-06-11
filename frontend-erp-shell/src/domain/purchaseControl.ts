export type PurchaseLineStatus =
  | 'to_order'
  | 'overdue'
  | 'no_date'
  | 'expected'
  | 'partial'
  | 'received'
  | 'closed'

export type PurchaseRow = {
  row_key: string
  line_id: number | null
  purchase_id: number | null
  order_id: number | null
  order_number: string
  order_date: string | null
  order_ref1c: string | null
  order_state_name: string | null
  source: 'mrp' | '1c'
  supplier_id: number | null
  supplier_name: string
  item_id: number
  item_code: string
  item_article: string | null
  item_name: string
  unit: string | null
  quantity: number
  received_qty: number
  remaining_qty: number
  delivery_date: string | null
  need_date: string | null
  overdue_days: number
  line_status: PurchaseLineStatus
  price: number
  amount: number
  run_id: number | null
}

export type PurchaseJournalSummary = {
  total_rows: number
  by_status: Record<string, number>
  to_order: number
  overdue: number
  expected_7d: number
  in_transit_amount: number
}

export type PurchaseOrdersResponse = {
  rows: PurchaseRow[]
  total: number
  limit: number
  offset: number
  run_id: number | null
  summary: PurchaseJournalSummary
}

export type PurchaseOrderCard = {
  order: {
    order_id: number
    order_number: string
    order_date: string | null
    order_ref1c: string | null
    order_state_name: string | null
    deletion_mark: boolean
    is_posted: boolean
    document_amount: number
    active: boolean
    source: 'mrp' | '1c'
    supplier_id: number | null
    supplier_name: string
  }
  lines: PurchaseRow[]
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
  active_only: boolean
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
}

export function purchaseLineStatusLabel(status: string): string {
  return purchaseLineStatusLabels[status as PurchaseLineStatus] ?? status
}

export function purchaseLineStatusPillClass(status: string): string {
  return purchaseLineStatusPillClasses[status as PurchaseLineStatus] ?? 'completed'
}

export const purchaseLineStatusOptions = (Object.entries(purchaseLineStatusLabels) as Array<[PurchaseLineStatus, string]>)
  .filter(([value]) => value !== 'closed')
