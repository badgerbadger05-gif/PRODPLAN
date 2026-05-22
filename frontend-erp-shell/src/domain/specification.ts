export type SpecNode = {
  id: string
  parentId: string | null
  type: 'item' | 'operation'
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
}
