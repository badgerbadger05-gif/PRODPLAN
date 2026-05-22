export type ProductionReportWeekDay = {
  date: string
  is_workday: boolean
  close_status?: string | null
  closed_planned?: number
  closed_fact?: number
  carry_qty?: number
}

export type ProductionReportWeekRow = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  plan_by_day: Record<string, number>
  fact_by_day: Record<string, number>
  carry_by_day?: Record<string, number>
  closed_plan_by_day?: Record<string, number>
  closed_fact_by_day?: Record<string, number>
  plan_week: number
  fact_week: number
  remaining_week: number
}

export type ProductionReportWeekResponse = {
  week_start: string
  days: ProductionReportWeekDay[]
  rows: ProductionReportWeekRow[]
  close_hint?: {
    today: string
    close_date: string
    target_date: string
  }
}

export type ProductionReportFactEntry = {
  item_id: number
  date: string
  fact_qty: number
}
