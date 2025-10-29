// Composable: данные Pegging (детальная таблица со связями parent-child)
// - Пагинация детальной таблицы через getPlanningResultPegging
// - Централизованная обработка ошибок (Quasar Notify)

import { ref, reactive } from 'vue'
import { Notify } from 'quasar'
import { getPlanningResultPegging } from '../services/api'
import { buildPlanRangeQuery } from '../services/query'
import type { PeggingRow, PeggingFilters, PageState } from '../types/mrp'

export function usePegging(runId: number) {
  // Фильтры
  const filters = ref<PeggingFilters>({
    child_item_id: undefined,
    parent_item_id: undefined,
    date_from: undefined,
    date_to: undefined
  })

  // Пагинация
  const pagination = reactive<PageState>({
    page: 1,
    rowsPerPage: 30,
    rowsNumber: 0
  })

  // Состояние
  const rows = ref<PeggingRow[]>([])
  const loadingPage = ref(false)

  // Детальная таблица (пагинация)
  async function loadPage() {
    loadingPage.value = true
    try {
      const params = buildPlanRangeQuery({
        date_from: filters.value.date_from,
        date_to: filters.value.date_to,
        page: pagination.page,
        limit: pagination.rowsPerPage
      })
      if (filters.value.child_item_id !== undefined && filters.value.child_item_id !== null) {
        params.child_item_id = filters.value.child_item_id
      }
      if (filters.value.parent_item_id !== undefined && filters.value.parent_item_id !== null) {
        params.parent_item_id = filters.value.parent_item_id
      }
      const resp = await getPlanningResultPegging(runId, params)
      const raw = resp.rows || []
      pagination.rowsNumber = resp.total || 0

      rows.value = raw.map((r: any) => ({
        id: Number(r.id),
        child_item_id: Number(r.child_item_id),
        parent_item_id: Number(r.parent_item_id),
        qty_contribution: Number(r.qty_contribution ?? 0),
        need_date: r.need_date ?? null,
        parent_need_date: r.parent_need_date ?? null
      }))
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки pegging',
        caption: e?.message || String(e)
      })
      rows.value = []
      pagination.rowsNumber = 0
    } finally {
      loadingPage.value = false
    }
  }

  function resetFilters() {
    filters.value.child_item_id = undefined
    filters.value.parent_item_id = undefined
    filters.value.date_from = undefined
    filters.value.date_to = undefined
  }

  function setFilters(next: Partial<PeggingFilters>) {
    filters.value = { ...filters.value, ...next }
  }

  return {
    // state
    filters,
    pagination,
    rows,
    loadingPage,
    // actions
    loadPage,
    resetFilters,
    setFilters
  }
}

export default usePegging