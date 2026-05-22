export type DistributedComponent = {
  item_id: number
  item_article?: string | null
  item_code: string
  item_name: string
  qty_per_unit: number
  stock_qty: number
  replenishment_method?: string | null
  norm_hours: number
  norm_hours_total: number
  stage_id?: number | null
  stage_name?: string | null
}

export type ProductDistributionBlock = {
  root_item_id: number
  root_item_code: string
  root_item_name: string
  components: DistributedComponent[]
}

export type ResourceDistributionResult = {
  resource_id: number
  resource_name: string
  norm_hours: number
  products: ProductDistributionBlock[]
}

export type ResourceDistributionResponse = {
  asOf?: string | null
  resources: ResourceDistributionResult[]
}
