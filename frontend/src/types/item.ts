export interface Item {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string
  item_ref1c?: string
  replenishment_method?: string
  replenishment_time?: number
  unit?: string
 stock_qty: number
  status: string
  created_at: string
  updated_at: string
}

export interface ItemCreate {
  item_code: string
  item_name: string
  item_article?: string
 item_ref1c?: string
  replenishment_method?: string
  replenishment_time?: number
  unit?: string
  stock_qty?: number
  status?: string
}

export interface ItemUpdate {
  item_code?: string
  item_name?: string
  item_article?: string
  item_ref1c?: string
  replenishment_method?: string
  replenishment_time?: number
  unit?: string
 stock_qty?: number
  status?: string
}