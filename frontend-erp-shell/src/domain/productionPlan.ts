export type PlanMatrixRow = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  month_plan: number
  days: Record<string, number>
}

export type PlanMatrixResponse = {
  rows: PlanMatrixRow[]
  dates: string[]
  total: number
  page: number
  page_size: number
}

export type PlanChange = {
  item_id: number
  date: string
  qty: number
  stage_id?: number | null
}

export type NomenclatureSearchItem = {
  item_code: string
  item_name: string
  item_article?: string | null
  similarity?: number | null
}

export type WeekInfo = {
  key: string
  label: string
  friday: string
  dates: string[]
}
