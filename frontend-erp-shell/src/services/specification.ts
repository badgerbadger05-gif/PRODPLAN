import type { SpecNode } from '../domain/specification'
import { api } from '../lib/api'

export function getSpecificationFull(params: { item_code: string; root_qty?: number; max_depth?: number }) {
  const search = new URLSearchParams()
  search.set('item_code', params.item_code)
  search.set('root_qty', String(params.root_qty ?? 1))
  search.set('max_depth', String(params.max_depth ?? 15))
  return api<{ nodes: SpecNode[]; meta: Record<string, unknown> }>(`/v1/specification/full?${search.toString()}`)
}
