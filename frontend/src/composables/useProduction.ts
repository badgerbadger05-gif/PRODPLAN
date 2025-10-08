// Composable: данные производства (детально + агрегаты + «повестка дня»)
// Цели:
// - Единый источник истины для вкладок «Производство»
// - Никаких двойных запросов: только пагинация для детальной таблицы и серверные агрегаты для верхних блоков
// - Централизованная обработка ошибок (Quasar Notify)
// - Догрузка справочников и денормализация item_name/article

import { ref, reactive } from 'vue'
import { Notify } from 'quasar'
import {
  getPlanningResultProduction,
  getPlanningResultProductionGrouped,
  getPlanningResultProductionAgendaDay,
  exportPlanningResultProduction
} from '../services/api'
import { useDictionaries } from './useDictionaries'
import { useFormatting } from './useFormatting'
import type {
  ProductionOrder,
  ProductionGroup,
  ProductionAgendaGroup,
  ProductionFilters,
  PageState,
  CsvOrXlsx
} from '../types/mrp'

// Утилита: '' -> undefined
function emptyToUndef(s?: string | null): string | undefined {
  const t = String(s ?? '').trim()
  return t.length ? t : undefined
}

export function useProduction(runId: number) {
  const { itemMap, ensureLoaded, fillMissingFromRows } = useDictionaries()
  const { formatNumber, formatQty } = useFormatting()

  // Фильтры
  const filters = ref<ProductionFilters>({
    bucket_type: undefined,
    date_from: undefined,
    date_to: undefined,
    day_date: undefined
  })

  // Пагинация детальной таблицы
  const pagination = reactive<PageState>({
    page: 1,
    rowsPerPage: 20,
    rowsNumber: 0
  })

  // Состояние
  const rows = ref<ProductionOrder[]>([])
  const grouped = ref<ProductionGroup[]>([])
  const agendaGroups = ref<ProductionAgendaGroup[]>([])

  const loadingPage = ref(false)
  const loadingGrouped = ref(false)
  const loadingAgenda = ref(false)
  const exporting = ref(false)

  // Денормализация по справочнику (название/артикул) + безопасные вычисления
  function enrichRows(src: any[]): ProductionOrder[] {
    const result: ProductionOrder[] = []
    for (const r of src || []) {
      const item = itemMap.value[r.item_id]
      const safeNormTotal = Number(r.norm_hours_total ?? 0)
      const safeQty = Number(r.qty ?? 0)
      const normPerUnit = r.norm_hours_per_unit != null
        ? Number(r.norm_hours_per_unit)
        : (safeQty > 0 ? safeNormTotal / safeQty : null)

      result.push({
        order_id: Number(r.order_id),
        item_id: Number(r.item_id),
        unit: r.unit ?? null,
        qty: Number(r.qty ?? 0),
        norm_hours_total: safeNormTotal,
        norm_hours_per_unit: normPerUnit,
        need_date: r.need_date ?? null,
        start_date: r.start_date ?? null,
        finish_date: r.finish_date ?? null,
        bucket_type: r.bucket_type ?? null,
        bucket_date: r.bucket_date ?? null,
        priority_index: r.priority_index ?? null,
        stages: Array.isArray(r.stages) ? r.stages : [],
        item_name: (item?.item_name ?? r.item_name ?? null),
        item_article: (item?.item_article ?? r.item_article ?? null)
      })
    }
    return result
  }

  // Загрузка детальной таблицы (пагинация)
  async function loadPage() {
    loadingPage.value = true
    try {
      const limit = pagination.rowsPerPage
      const offset = (pagination.page - 1) * pagination.rowsPerPage
      const resp = await getPlanningResultProduction(runId, {
        bucket_type: filters.value.bucket_type,
        date_from: emptyToUndef(filters.value.date_from),
        date_to: emptyToUndef(filters.value.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc',
        limit,
        offset
      })
      const raw = resp.rows || []
      pagination.rowsNumber = resp.total || 0

      // Справочники: первичная загрузка + догрузка недостающих из фактических строк
      await ensureLoaded()
      await fillMissingFromRows(raw)

      rows.value = enrichRows(raw)
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки производственных заказов',
        caption: e?.message || String(e)
      })
      rows.value = []
      pagination.rowsNumber = 0
    } finally {
      loadingPage.value = false
    }
  }

  // Серверная группировка по видам/участкам
  async function loadGrouped() {
    loadingGrouped.value = true
    try {
      const resp = await getPlanningResultProductionGrouped(runId, {
        bucket_type: filters.value.bucket_type,
        date_from: emptyToUndef(filters.value.date_from),
        date_to: emptyToUndef(filters.value.date_to),
        limit: 1000,
        offset: 0,
        sort_by: 'item_name',
        sort_dir: 'asc'
      })
      grouped.value = (resp?.groups || []).map(g => ({
        area_id: Number(g.area_id),
        area_name: String(g.area_name ?? ''),
        orders: (g.orders || []).map(o => ({
          agg_key: String(o.agg_key ?? `${o.item_id}|${o.unit || ''}`),
          item_id: Number(o.item_id),
          item_name: o.item_name ?? (itemMap.value[o.item_id]?.item_name ?? `Номенклатура #${o.item_id}`),
          item_article: o.item_article ?? (itemMap.value[o.item_id]?.item_article ?? ''),
          unit: o.unit ?? null,
          qty: Number(o.qty ?? 0),
          norm_hours_total: Number(o.norm_hours_total ?? 0),
          norm_hours_per_unit: o.norm_hours_per_unit != null ? Number(o.norm_hours_per_unit) : null
        })),
        norm_sum_hours: Number(g.norm_sum_hours ?? 0),
        min_days_to_need: g.min_days_to_need != null ? Number(g.min_days_to_need) : null,
        cap_overload_hours: Number(g.cap_overload_hours ?? 0),
        cap_overloaded_buckets: Number(g.cap_overloaded_buckets ?? 0)
      }))
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки группировки производства',
        caption: e?.message || String(e)
      })
      grouped.value = []
    } finally {
      loadingGrouped.value = false
    }
  }

  // «Повестка дня» (daily) по видам/участкам
  async function loadAgendaDay() {
    loadingAgenda.value = true
    try {
      const day = (filters.value.day_date || '')?.slice(0, 10)
      if (!day) {
        agendaGroups.value = []
        return
      }
      const resp = await getPlanningResultProductionAgendaDay(runId, { day_date: day })
      agendaGroups.value = (resp?.groups || []).map(g => ({
        area_id: Number(g.area_id),
        area_name: String(g.area_name ?? ''),
        orders: (g.orders || []).map(o => ({
          agg_key: String(o.agg_key ?? `${o.item_id}|${o.unit || ''}`),
          item_id: Number(o.item_id),
          item_name: o.item_name ?? (itemMap.value[o.item_id]?.item_name ?? `Номенклатура #${o.item_id}`),
          item_article: o.item_article ?? (itemMap.value[o.item_id]?.item_article ?? ''),
          unit: o.unit ?? null,
          qty: Number(o.qty ?? 0),
          norm_hours_total: Number(o.norm_hours_total ?? 0),
          norm_hours_per_unit: o.norm_hours_per_unit != null ? Number(o.norm_hours_per_unit) : null
        })),
        norm_sum_hours: Number(g.norm_sum_hours ?? 0),
        sum_qty: Number(g.sum_qty ?? 0),
        cap_overload_hours: Number(g.cap_overload_hours ?? 0)
      }))
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки повестки дня',
        caption: e?.message || String(e)
      })
      agendaGroups.value = []
    } finally {
      loadingAgenda.value = false
    }
  }

  // Экспорт CSV/XLSX
  function downloadTextFile(content: string, filename: string, mime: string) {
    const blob = new Blob([content], { type: mime })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  function downloadBase64Xlsx(b64: string, filename: string) {
    const byteChars = atob(b64 || '')
    const byteNumbers = new Array(byteChars.length)
    for (let i = 0; i < byteChars.length; i++) {
      byteNumbers[i] = byteChars.charCodeAt(i)
    }
    const byteArray = new Uint8Array(byteNumbers)
    const blob = new Blob([byteArray], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  async function exportProd(fmt: CsvOrXlsx) {
    exporting.value = true
    try {
      const res = await exportPlanningResultProduction(runId, {
        format: fmt,
        bucket_type: filters.value.bucket_type,
        date_from: emptyToUndef(filters.value.date_from),
        date_to: emptyToUndef(filters.value.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc'
      })
      if (fmt === 'csv') {
        downloadTextFile(res?.data || '', res?.filename || `mrp_production_run_${runId}.csv`, 'text/csv;charset=utf-8')
      } else {
        downloadBase64Xlsx(res?.data_base64 || '', res?.filename || `mrp_production_run_${runId}.xlsx`)
      }
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка экспорта производственных заказов',
        caption: e?.message || String(e)
      })
    } finally {
      exporting.value = false
    }
  }

  function resetFilters() {
    filters.value.bucket_type = undefined
    filters.value.date_from = undefined
    filters.value.date_to = undefined
    filters.value.day_date = undefined
  }

  function setFilters(next: Partial<ProductionFilters>) {
    filters.value = { ...filters.value, ...next }
  }

  return {
    // state
    filters,
    pagination,
    rows,
    grouped,
    agendaGroups,
    loadingPage,
    loadingGrouped,
    loadingAgenda,
    exporting,
    // actions
    loadPage,
    loadGrouped,
    loadAgendaDay,
    exportProd,
    resetFilters,
    setFilters,
    // helpers for UI formatting if нужно
    formatNumber,
    formatQty
  }
}

export default useProduction