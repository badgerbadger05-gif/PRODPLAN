// Composable: данные закупок (детально + агрегат)
// - Единый источник истины для вкладки «Закупки»
// - Только пагинация для детальной таблицы и серверная агрегация для верхнего блока
// - Централизованная обработка ошибок (Quasar Notify)
// - Догрузка справочников и денормализация item_name/article

import { ref, reactive } from 'vue'
import { Notify } from 'quasar'
import {
  getPlanningResultPurchases,
  getPlanningResultPurchasesGrouped,
  exportPlanningResultPurchases
} from '../services/api'
import { useDictionaries } from './useDictionaries'
import { useFormatting } from './useFormatting'
import type {
  PurchaseRow,
  PurchaseGroupedRow,
  PurchaseFilters,
  PageState,
  CsvOrXlsx
} from '../types/mrp'

// '' -> undefined
function emptyToUndef(s?: string | null): string | undefined {
  const t = String(s ?? '').trim()
  return t.length ? t : undefined
}

export function usePurchases(runId: number) {
  const { itemMap, ensureLoaded, fillMissingFromRows } = useDictionaries()
  const { formatNumber, formatQty } = useFormatting()

  // Фильтры
  const filters = ref<PurchaseFilters>({
    bucket_type: undefined,
    date_from: undefined,
    date_to: undefined
  })

  // Пагинация
  const pagination = reactive<PageState>({
    page: 1,
    rowsPerPage: 20,
    rowsNumber: 0
  })

  // Состояние
  const rows = ref<PurchaseRow[]>([])
  const grouped = ref<PurchaseGroupedRow[]>([])

  const loadingPage = ref(false)
  const loadingGrouped = ref(false)
  const exporting = ref(false)

  function enrichRows(src: any[]): PurchaseRow[] {
    const result: PurchaseRow[] = []
    for (const r of src || []) {
      const item = itemMap.value[r.item_id]
      result.push({
        purchase_id: Number(r.purchase_id),
        item_id: Number(r.item_id),
        unit: r.unit ?? null,
        qty: Number(r.qty ?? 0),
        need_date: r.need_date ?? null,
        order_date: r.order_date ?? null,
        lead_time_days: r.lead_time_days != null ? Number(r.lead_time_days) : null,
        bucket_type: r.bucket_type ?? null,
        bucket_date: r.bucket_date ?? null,
        priority_index: r.priority_index != null ? Number(r.priority_index) : null,
        item_name: (item?.item_name ?? r.item_name ?? null),
        item_article: (item?.item_article ?? r.item_article ?? null)
      })
    }
    return result
  }

  // Детальная таблица (пагинация)
  async function loadPage() {
    loadingPage.value = true
    try {
      const limit = pagination.rowsPerPage
      const offset = (pagination.page - 1) * pagination.rowsPerPage
      const resp = await getPlanningResultPurchases(runId, {
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

      // Справочники
      await ensureLoaded()
      await fillMissingFromRows(raw)

      rows.value = enrichRows(raw)
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки заявок на закупку',
        caption: e?.message || String(e)
      })
      rows.value = []
      pagination.rowsNumber = 0
    } finally {
      loadingPage.value = false
    }
  }

  // Агрегированная таблица (item_id+unit)
  async function loadGrouped() {
    loadingGrouped.value = true
    try {
      const resp = await getPlanningResultPurchasesGrouped(runId, {
        bucket_type: filters.value.bucket_type,
        date_from: emptyToUndef(filters.value.date_from),
        date_to: emptyToUndef(filters.value.date_to),
        limit: 1000,
        offset: 0
      })
      grouped.value = (resp?.rows || []).map(r => ({
        agg_key: String(r.agg_key ?? `${r.item_id}|${r.unit || ''}`),
        item_id: Number(r.item_id),
        item_name: r.item_name ?? (itemMap.value[r.item_id]?.item_name ?? `Номенклатура #${r.item_id}`),
        item_article: r.item_article ?? (itemMap.value[r.item_id]?.item_article ?? ''),
        unit: r.unit ?? null,
        qty: Number(r.qty ?? 0)
      }))
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки агрегированных данных закупок',
        caption: e?.message || String(e)
      })
      grouped.value = []
    } finally {
      loadingGrouped.value = false
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

  async function exportPurch(fmt: CsvOrXlsx) {
    exporting.value = true
    try {
      const res = await exportPlanningResultPurchases(runId, {
        format: fmt,
        bucket_type: filters.value.bucket_type,
        date_from: emptyToUndef(filters.value.date_from),
        date_to: emptyToUndef(filters.value.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc'
      })
      if (fmt === 'csv') {
        downloadTextFile(res?.data || '', res?.filename || `mrp_purchases_run_${runId}.csv`, 'text/csv;charset=utf-8')
      } else {
        downloadBase64Xlsx(res?.data_base64 || '', res?.filename || `mrp_purchases_run_${runId}.xlsx`)
      }
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка экспорта заявок на закупку',
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
  }

  function setFilters(next: Partial<PurchaseFilters>) {
    filters.value = { ...filters.value, ...next }
  }

  return {
    // state
    filters,
    pagination,
    rows,
    grouped,
    loadingPage,
    loadingGrouped,
    exporting,
    // actions
    loadPage,
    loadGrouped,
    exportPurch,
    resetFilters,
    setFilters,
    // helpers for UI formatting if нужно
    formatNumber,
    formatQty
  }
}

export default usePurchases