import axios from 'axios'

// В контейнере фронтенда все запросы идут через Nginx-прокси на backend:
// nginx.conf: location /api/ { proxy_pass http://backend:8000/api/; }
const api = axios.create({
  baseURL: '/api',
  timeout: 900000, // 15 минут для длительных операций (спецификации, этапы и т.п.)
})

export interface ApiResponse<T> {
  data: T
  success: boolean
  message?: string
}

export default api
// Specification Tree API typing and helper

export type SpecStage = {
  id: string | number
  name: string
} | null

export type SpecOperationInfo = {
  id: string | number | null
  name: string | null
} | null

export type SpecComputed = {
  treeQty?: number | null
  treeTimeNh?: number | null
}

export type SpecNode = {
  id: string
  parentId: string | null
  type: 'item' | 'operation'
  name: string | null
  article: string | null
  stage: SpecStage
  operation: SpecOperationInfo
  qtyPerParent: number | null
  unit: string | null
  replenishmentMethod?: string | null
  timeNormNh: number | null
  computed?: SpecComputed
  hasChildren: boolean
  warnings: string[]
  item?: { id: number; code: string }
  // For QTable tree
  children?: SpecNode[]
  __loading__?: boolean
}

export async function getSpecificationTree(params: {
  item_code?: string
  item_id?: number
  root_qty?: number
  parent_id?: string
  depth?: number
}): Promise<{ nodes: SpecNode[]; meta: any }> {
  const { data } = await api.get('/v1/specification/tree', { params })
  return data
}
export async function getSpecificationFull(params: {
  item_code?: string
  item_id?: number
  root_qty?: number
  max_depth?: number
}): Promise<{ nodes: SpecNode[]; meta: any }> {
  const { data } = await api.get('/v1/specification/full', { params })
  return data
}