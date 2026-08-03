export type SpecNode = {
  id: string
  parentId: string | null
  type: 'item' | 'operation'
  // componentId — PK строки состава (для ремонта: restage/move). null у корня.
  componentId?: number | null
  // specId — собственная спека этой номенклатуры (куда добавлять компоненты). null, если спеки нет.
  specId?: number | null
  name?: string | null
  article?: string | null
  stage?: { id?: string | number | null; name?: string | null } | null
  operation?: { id?: string | number | null; name?: string | null } | null
  qtyPerParent?: number | null
  unit?: string | null
  replenishmentMethod?: string | null
  timeNormNh?: number | null
  computed?: { treeQty?: number | null; treeTimeNh?: number | null }
  hasChildren?: boolean
  warnings?: string[]
  children?: SpecNode[]
}

export type SpecFlatRow = SpecNode & {
  level: number
  path?: string[]
}

export type BomItem = ApiSchemas['SpecificationSearchItemResponse']
export type BomSearchResponse = ApiSchemas['SpecificationSearchResponse']
export type BomItemIdentity = Pick<BomItem, 'item_id' | 'item_code' | 'item_name'>
  & Partial<Omit<BomItem, 'item_id' | 'item_code' | 'item_name'>>

export type BomFlattenedItem = {
  item_id: number
  item_code: string
  article?: string | null
  name: string
  unit?: string | null
  replenishment_method?: string | null
  total_qty: number
  occurrences: number
  levels: number[]
  stages: string[]
  paths: Array<{ level: number; qty: number; path: string }>
  warnings: string[]
  has_children?: boolean
}

export type BomFlattenedResponse = {
  items: BomFlattenedItem[]
  meta: { root?: BomItem; count?: number; root_qty?: number }
}

export type BomWhereUsedItem = {
  parent: BomItem
  spec: {
    spec_id: number
    spec_code?: string | null
    spec_name?: string | null
    spec_ref1c?: string | null
  }
  component_item_id: number
  qty_per_parent: number
  total_qty_to_target: number
  level_up: number
  stage?: { id: number; name: string } | null
  path: Array<{ item_id: number; article?: string | null; name: string }>
}

export type BomWhereUsedResponse = {
  items: BomWhereUsedItem[]
  meta: { target?: BomItem; count?: number; max_depth?: number }
}

export type BomQualityIssue = {
  code: string
  severity: 'error' | 'warning' | 'info'
  message: string
  item?: {
    item_id: number
    item_code: string
    item_article?: string | null
    item_name: string
  } | null
  spec_id?: number | null
}

export type BomQualityResponse = {
  issues: BomQualityIssue[]
  meta: { root?: BomItem; count?: number }
}
import type { components } from '../lib/apiTypes'

type ApiSchemas = components['schemas']
