export type NomenclatureSearchItem = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
  similarity?: number | null
}

export type NomenclatureSearchResponse = {
  items: NomenclatureSearchItem[]
  total: number
  query: string
  search_type: string
}
