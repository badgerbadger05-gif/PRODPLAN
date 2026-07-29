import type { NomenclatureSearchResponse } from '../domain/nomenclature'
import { api } from '../lib/api'

// Справочник номенклатуры ведёт синхронизация из 1С. UI только ищет по нему:
// ни создавать, ни переименовывать позиции фронтенд не может.
export function searchNomenclature(query: string, limit = 12) {
  const search = new URLSearchParams()
  search.set('q', query)
  search.set('limit', String(limit))
  return api<NomenclatureSearchResponse>(`/v1/nomenclature/search?${search.toString()}`)
}
