// Composable: данные мощностей (детально + сводка)
// - Детальная таблица (пагинация) через getPlanningResultCapacity
// - Сводка по участкам через getPlanningResultCapacitySummary
// - Централизованная обработка ошибок (Quasar Notify)

import { ref, reactive } from 'vue'
import { Notify } from 'quasar'
import {
  getPlanningResultCapacity,
  getPlanningResultCapacitySummary
} from '../services/api'
import { useFormatting } from './useFormatting'
import { buildPlanRangeQuery } from '../services/query'
import type {
  CapacityRow,
  CapacitySummaryMap,
  CapacityFilters,
  PageState
} from '../types/mrp'

export function useCapacity(runId: number) {
  const { formatNumber } = useFormatting()

  // Фильтры
  const filters = ref<CapacityFilters>({
    date_from: undefined,
    date_to: undefined,
    area_id: undefined
  })

  // Пагинация
  const pagination = reactive<PageState>({
    page: 1,
    rowsPerPage: 30,
    rowsNumber: 0
  })

  // Состояние
  const rows = ref<CapacityRow[]>([])
  const summaryMap = ref<CapacitySummaryMap>({})

  const loadingPage = ref(false)
  const loadingSummary = ref(false)

  // Детальная таблица мощностей (пагинация)
  async function loadPage() {
    loadingPage.value = true
    try {
      const params = buildPlanRangeQuery({
        date_from: filters.value.date_from,
        date_to: filters.value.date_to,
        page: pagination.page,
        limit: pagination.rowsPerPage
      })
      if (filters.value.area_id !== undefined && filters.value.area_id !== null) {
        params.area_id = filters.value.area_id
      }
      const resp = await getPlanningResultCapacity(runId, params)
      const raw = resp.rows || []
      pagination.rowsNumber = resp.total || 0

      rows.value = raw.map((r: any) => ({
        area_id: Number(r.area_id),
        bucket_type: r.bucket_type ?? null,
        bucket_date: r.bucket_date ?? null,
        hours_planned: Number(r.hours_planned ?? 0),
        hours_available: Number(r.hours_available ?? 0),
        overload_hours: Number(r.overload_hours ?? 0)
      }))
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки данных по мощностям',
        caption: e?.message || String(e)
      })
      rows.value = []
      pagination.rowsNumber = 0
    } finally {
      loadingPage.value = false
    }
  }

  // Сводка по мощностям (карта по area_id)
  async function loadSummary() {
    loadingSummary.value = true
    try {
      const params = buildPlanRangeQuery({
        date_from: filters.value.date_from,
        date_to: filters.value.date_to
      })
      const resp = await getPlanningResultCapacitySummary(runId, params)
      const m = (resp?.map || {}) as any
      const map: CapacitySummaryMap = {}
      for (const k of Object.keys(m)) {
        const aid = Number(k)
        const v = m[k] || {}
        map[aid] = {
          hours_planned: Number(v.hours_planned ?? 0),
          hours_available: Number(v.hours_available ?? 0),
          overload_hours: Number(v.overload_hours ?? 0),
          overloaded_buckets: Number(v.overloaded_buckets ?? 0)
        }
      }
      summaryMap.value = map
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки сводки мощностей',
        caption: e?.message || String(e)
      })
      summaryMap.value = {}
    } finally {
      loadingSummary.value = false
    }
  }

  function resetFilters() {
    filters.value.date_from = undefined
    filters.value.date_to = undefined
    filters.value.area_id = undefined
  }

  function setFilters(next: Partial<CapacityFilters>) {
    filters.value = { ...filters.value, ...next }
  }

  return {
    // state
    filters,
    pagination,
    rows,
    summaryMap,
    loadingPage,
    loadingSummary,
    // actions
    loadPage,
    loadSummary,
    resetFilters,
    setFilters,
    // helpers if UI needs
    formatNumber
  }
}

export default useCapacity