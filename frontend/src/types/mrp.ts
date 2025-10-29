// Типы домена MRP для фронтенда (Vue/Quasar). Единая точка правды для данных страниц результатов прогона.

export type BucketType = 'daily' | 'weekly'
export type IsoDate = string // 'YYYY-MM-DD'

// ---------- Summary ----------
export interface MRPSummaryRun {
  run_id: number
  status: string
  started_at: string | null
  finished_at: string | null
  horizon_days?: number | null
  use_weekly?: boolean
}

export interface MRPSummaryCounts {
  production_orders?: number
  purchase_requests?: number
}

export interface MRPSummaryCapacity {
  overloaded_buckets?: number
  overload_total?: number
  hours_planned_total?: number
  hours_available_total?: number
}

export interface MRPSummary {
  run: MRPSummaryRun
  counts?: MRPSummaryCounts
  capacity?: MRPSummaryCapacity
  kpi?: any
  warnings: any[]
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
  bucket_type?: BucketType | null
  bucket_date?: IsoDate | null
  hours?: number | null
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
  bucket_type?: BucketType | null
  bucket_date?: IsoDate | null
  priority_index?: number | null
  stages?: ProductionStage[]
  // денормализованные поля для UI
  item_name?: string | null
  item_article?: string | null
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
  // agenda_day-specific extended fields (optional)
  order_id?: number
  display_qty?: number
  display_norm_hours_total?: number
  overload?: boolean
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
  bucket_type?: BucketType | null
  bucket_date?: IsoDate | null
  priority_index?: number | null
  // денормализованные поля для UI
  item_name?: string | null
  item_article?: string | null
}

export interface PurchaseGroupedRow {
  agg_key: string
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
}

// ---------- Capacity ----------
export interface CapacityRow {
  area_id: number
  bucket_type?: BucketType | null
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
export type CsvOrXlsx = 'csv' | 'xlsx'