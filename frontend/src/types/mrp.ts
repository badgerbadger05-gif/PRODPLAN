// Типы домена MRP для фронтенда (Vue/Quasar). Единая точка правды для данных страниц результатов прогона.

export type BucketType = 'daily'
export type IsoDate = string // 'YYYY-MM-DD'

// Бэкенд предупреждения/флаги
export type WarningCode =
  | 'COMPONENT_SHORTAGE_BLOCKED'
  | 'COMPONENT_SHORTAGE_PARTIAL'
  | 'CAPACITY_UNSCHEDULED'
  | 'CAPACITY_SHIFTED'
  | 'NO_AREA_FOR_PRODUCTION_KIND'
  | 'NO_PRODUCTION_KIND'
  | 'NO_TIME_NORM'
  | string

export type WarningEntry = {
  code: WarningCode
  msg?: string
  [k: string]: any
}

// ---------- Summary ----------
export interface MRPSummaryRun {
  run_id: number
  status: string
  started_at: string | null
  finished_at: string | null
  horizon_days?: number | null
}

export interface MRPSummaryCounts {
  production_orders?: number
  purchase_requests?: number
  rework_requests?: number
}

export interface MRPSummaryCapacity {
  overloaded_buckets?: number
  overload_total?: number
  hours_planned_total?: number
  hours_available_total?: number
}

export interface MRPSummaryKindIssues {
  total: number
  byCode?: Record<string, number>
  list?: WarningEntry[]
}

export interface MRPSummaryMissingNorms {
  total: number
}

export interface MRPSummaryComponentShortages {
  blocked: number
  partial: number
}

export interface MRPSummary {
  run: MRPSummaryRun
  counts?: MRPSummaryCounts
  capacity?: MRPSummaryCapacity
  kpi?: any
  warnings: WarningEntry[]
  // Новые структурированные агрегаты
  kindIssues?: MRPSummaryKindIssues
  missingNorms?: MRPSummaryMissingNorms
  componentShortages?: MRPSummaryComponentShortages
}

// ---------- Справочники ----------
export interface ItemInfo {
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
}

export interface ResourceInfo {
  resource_id: number
  resource_name: string
}

export type ItemMap = Record<number, ItemInfo>
export type AreaMap = Record<number, string>

// ---------- Production ----------
export interface ProductionStage {
  stage_id: number | string
  area_id?: number | null
  area_name?: string | null
  bucket_type?: BucketType | null // NOTE: Now always 'daily' - keeping for compatibility with existing data structures
  bucket_date?: IsoDate | null
  hours?: number | null
  // Признак отсутствия норматива на этапе (hours ~ 0)
  missingNorm?: boolean
}

export interface ProductionFlags {
  missingArea?: boolean
  missingNorm?: boolean
  componentBlocked?: boolean
  componentPartial?: boolean
  capacityShiftDays?: number
}

export interface ProductionOrder {
  order_id: number
  item_id: number
  unit?: string | null
  qty: number
  norm_hours_total: number
  norm_hours_per_unit?: number | null
  need_date?: string | null
  start_date?: string | null
  finish_date?: string | null
  bucket_type?: BucketType | null // NOTE: Now always 'daily' - keeping for compatibility with existing data structures
  bucket_date?: IsoDate | null
  priority_index?: number | null
  stages?: ProductionStage[]
  // денормализованные поля для UI
  item_name?: string | null
  item_article?: string | null
  // Основной участок для строки заказа (по этапу с максимумом часов)
  main_area_id?: number | null
  main_area_name?: string | null
  badge?: string | null
  turning_blank_priority?: boolean
  // Флаги для отрисовки плашек/индикаторов
  flags?: ProductionFlags
  source_order_ids?: number[]
}

export interface ProductionGroupOrder {
  agg_key: string
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  norm_hours_total: number
  norm_hours_per_unit?: number | null
  order_id?: number
  display_qty?: number
  display_norm_hours_total?: number
  overload?: boolean
  badge?: string | null
  turning_blank_priority?: boolean
  source_order_ids?: number[]
}

export interface ProductionGroup {
  area_id: number
  area_name: string
  orders: ProductionGroupOrder[]
  norm_sum_hours: number
  min_days_to_need?: number | null
  cap_overload_hours?: number
  cap_overloaded_buckets?: number
}

export interface ProductionAgendaGroup {
  area_id: number
  area_name: string
  orders: ProductionGroupOrder[]
  norm_sum_hours: number
  sum_qty: number
  cap_overload_hours?: number
  // extended capacity info for day agenda
  hours_available_day?: number
  cap_overload_percent?: number | null
}

// ---------- Purchases ----------
export interface PurchaseRow {
  purchase_id: number
  item_id: number
  unit?: string | null
  qty: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  bucket_type?: BucketType | null // NOTE: Now always 'daily' - keeping for compatibility with existing data structures
  bucket_date?: IsoDate | null
  priority_index?: number | null
  // денормализованные поля для UI
  item_name?: string | null
  item_article?: string | null
  badge?: string | null
  turning_blank_priority?: boolean
  late_supplier_order?: boolean
}

export interface PurchaseGroupedRow {
  agg_key: string
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  badge?: string | null
  turning_blank_priority?: boolean
  late_supplier_order?: boolean
}

export interface PurchaseCategoryGroupOrder {
  purchase_id: number
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  bucket_type?: BucketType | null
  bucket_date?: IsoDate | null
  priority_index?: number | null
  supplier_ref1c?: string | null
  source_purchase_ids?: number[]
  badge?: string | null
  turning_blank_priority?: boolean
  late_supplier_order?: boolean
}

export interface PurchaseCategoryGroup {
  group_id?: number | null
  group_name: string
  orders: PurchaseCategoryGroupOrder[]
  sum_qty: number
}

export interface PurchaseCategoryGroupedResponse {
  groups: PurchaseCategoryGroup[]
  total_groups: number
  total_orders: number
  limit: number
  offset: number
}

export interface ReworkRow {
  rework_id: number
  item_id: number
  unit?: string | null
  qty: number
  requested_qty: number
  planned_qty: number
  need_date?: string | null
  order_date?: string | null
  lead_time_days?: number | null
  bucket_type?: BucketType | null
  bucket_date?: IsoDate | null
  priority_index?: number | null
  spec_id?: number | null
  spec_code?: string | null
  spec_name?: string | null
  component_limit?: number | null
  component_blocked?: boolean
  component_partial?: boolean
  shortage?: Record<string, any> | null
  item_name?: string | null
  item_article?: string | null
}

export interface ReworkGroup {
  group_id?: number | null
  group_name: string
  orders: ReworkRow[]
  sum_qty: number
  sum_requested_qty: number
  sum_planned_qty: number
  blocked_orders: number
  partial_orders: number
}

export interface ReworkGroupedResponse {
  groups: ReworkGroup[]
  total_groups: number
  total_orders: number
  limit: number
  offset: number
}

// ---------- Capacity ----------
export interface CapacityRow {
  area_id: number
  bucket_type?: BucketType | null // NOTE: Now always 'daily' - keeping for compatibility with existing data structures
  bucket_date?: IsoDate | null
  hours_planned: number
  hours_available: number
  overload_hours: number
}

export interface CapacitySummary {
  hours_planned: number
  hours_available: number
  overload_hours: number
  overloaded_buckets: number
}

export type CapacitySummaryMap = Record<number, CapacitySummary>

// ---------- Pegging ----------
export interface PeggingRow {
  id: number
  child_item_id: number
  parent_item_id: number
  qty_contribution: number
  need_date?: IsoDate | null
  parent_need_date?: IsoDate | null
}

// ---------- Фильтры ----------
export interface ProductionFilters {
 date_from?: IsoDate
  date_to?: IsoDate
}

export interface PurchaseFilters {
  date_from?: IsoDate
  date_to?: IsoDate
}

export interface ReworkFilters {
  date_from?: IsoDate
  date_to?: IsoDate
}

export interface CapacityFilters {
  date_from?: IsoDate
  date_to?: IsoDate
  area_id?: number
}

export interface PeggingFilters {
  child_item_id?: number
  parent_item_id?: number
  date_from?: IsoDate
  date_to?: IsoDate
}

// ---------- Пагинация ----------
export interface PageState {
  page: number
  rowsPerPage: number
  rowsNumber: number
}

export interface PagedResponse<T> {
  rows: T[]
  total: number
  limit: number
  offset: number
}

// ---------- Экспортные типы ----------
// ---------- Query-параметры ----------
// Импортируем из query.ts для совместимости
export type { PlanRangeParams, PaginationParams, SortingParams, PlanQueryParams } from '../services/query'
export type CsvOrXlsx = 'csv' | 'xlsx'
