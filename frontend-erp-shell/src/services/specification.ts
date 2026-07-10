import type {
  BomFlattenedResponse,
  BomQualityResponse,
  BomSearchResponse,
  BomWhereUsedResponse,
  SpecNode,
} from '../domain/specification'
import { api } from '../lib/api'

type ExportResponse = {
  data_base64?: string
  filename?: string
  content_type?: string
  format?: string
}

export function getSpecificationFull(params: { item_code?: string; item_id?: number; root_qty?: number; max_depth?: number }) {
  const search = new URLSearchParams()
  if (params.item_id != null) search.set('item_id', String(params.item_id))
  if (params.item_code) search.set('item_code', params.item_code)
  search.set('root_qty', String(params.root_qty ?? 1))
  search.set('max_depth', String(params.max_depth ?? 15))
  return api<{ nodes: SpecNode[]; meta: Record<string, unknown> }>(`/v1/specification/full?${search.toString()}`)
}

export function searchSpecificationItems(params: { q: string; limit?: number; has_spec?: boolean; quality?: string }) {
  const search = new URLSearchParams()
  search.set('q', params.q)
  search.set('limit', String(params.limit ?? 50))
  if (params.has_spec != null) search.set('has_spec', String(params.has_spec))
  if (params.quality) search.set('quality', params.quality)
  return api<BomSearchResponse>(`/v1/specification/search?${search.toString()}`)
}

export function getSpecificationFlattened(params: { item_id?: number; item_code?: string; root_qty?: number; max_depth?: number }) {
  const search = new URLSearchParams()
  if (params.item_id != null) search.set('item_id', String(params.item_id))
  if (params.item_code) search.set('item_code', params.item_code)
  search.set('root_qty', String(params.root_qty ?? 1))
  search.set('max_depth', String(params.max_depth ?? 15))
  return api<BomFlattenedResponse>(`/v1/specification/flattened?${search.toString()}`)
}

export function getSpecificationWhereUsed(params: { item_id?: number; item_code?: string; max_depth?: number }) {
  const search = new URLSearchParams()
  if (params.item_id != null) search.set('item_id', String(params.item_id))
  if (params.item_code) search.set('item_code', params.item_code)
  search.set('max_depth', String(params.max_depth ?? 8))
  return api<BomWhereUsedResponse>(`/v1/specification/where-used?${search.toString()}`)
}

export function getSpecificationQuality(params: { item_id?: number; item_code?: string; max_depth?: number }) {
  const search = new URLSearchParams()
  if (params.item_id != null) search.set('item_id', String(params.item_id))
  if (params.item_code) search.set('item_code', params.item_code)
  search.set('max_depth', String(params.max_depth ?? 15))
  return api<BomQualityResponse>(`/v1/specification/quality?${search.toString()}`)
}

export function exportSpecificationXlsx(params: { item_id?: number; item_code?: string; root_qty?: number; max_depth?: number; replenishment_method?: string }) {
  const search = new URLSearchParams()
  if (params.item_id != null) search.set('item_id', String(params.item_id))
  if (params.item_code) search.set('item_code', params.item_code)
  search.set('root_qty', String(params.root_qty ?? 1))
  search.set('max_depth', String(params.max_depth ?? 20))
  if (params.replenishment_method) search.set('replenishment_method', params.replenishment_method)
  return api<ExportResponse>(`/v1/specification/export?${search.toString()}`)
}
