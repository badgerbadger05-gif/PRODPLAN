export type FeasibilityStatus = 'ok' | 'make' | 'shortage' | 'blocked' | 'non_stock' | 'not_required'

export type FeasibilityItem = {
  item_id: number
  item_code: string
  item_article: string
  item_name: string
  unit?: string | null
  stock_on_hand?: number | null
  has_spec?: boolean
}

export type FeasibilitySearchResponse = {
  items: FeasibilityItem[]
  meta: { q?: string; count?: number }
}

export type FeasibilityWarehouse = {
  warehouse_name: string
  qty: number
  /** false — склад исключён настройками, его остаток в покрытие не идёт. */
  counted: boolean
}

export type FeasibilityParent = {
  item_id: number
  item_article: string
  item_name: string
}

export type FeasibilityRow = {
  item_id: number
  item_code: string
  item_article: string
  item_name: string
  unit?: string | null
  replenishment_method?: string | null
  level: number
  kind: 'node' | 'material'
  status: FeasibilityStatus
  /** Одна строка: чем позицию закрыть. */
  reason: string
  /** Срок пополнения из 1С, дней. */
  replenishment_time?: number | null
  /** false — это количество позицию не требует: ветка закрыта остатком выше. */
  needed_now: boolean
  is_blocking: boolean
  required_qty: number
  stock_on_hand: number
  allocated_qty: number
  shortage_qty: number
  used_in: FeasibilityParent[]
  warehouses: FeasibilityWarehouse[]
}

export type FeasibilityTreeNode = {
  key: string
  item_id: number
  item_code: string
  item_article: string
  item_name: string
  unit?: string | null
  replenishment_method?: string | null
  level: number
  kind: 'node' | 'material'
  status: FeasibilityStatus
  reason: string
  replenishment_time?: number | null
  /** Остатка не хватает на потребность ветки — цифру остатка красим. */
  stock_short: boolean
  qty_per_parent: number | null
  branch_required_qty: number
  branch_shortage_qty: number
  required_qty: number
  stock_on_hand: number
  shortage_qty: number
  has_children: boolean
  children: FeasibilityTreeNode[]
}

export type FeasibilityRoot = {
  item_id: number
  item_code: string
  item_article: string
  item_name: string
  unit?: string | null
  replenishment_method?: string | null
  requested_qty: number
  stock_on_hand: number
  has_spec: boolean
}

export type FeasibilitySummary = {
  status: FeasibilityStatus
  shortage_count: number
  blocked_count: number
  make_count: number
  /** Красные, которые это количество не требует. */
  idle_blocker_count: number
  items_checked: number
  max_level: number
  producible_qty: number
  fully_producible: boolean
  warnings: string[]
  cycles: string[]
  max_depth: number
}

export type FeasibilityResponse = {
  root: FeasibilityRoot
  summary: FeasibilitySummary
  blocking: FeasibilityRow[]
  tree: FeasibilityTreeNode | null
  tree_truncated: boolean
}

export type FeasibilityCandidate = {
  item_id: number
  item_code: string
  item_article: string
  item_name: string
}
