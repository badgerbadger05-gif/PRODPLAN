// Pinia store: централизованное состояние результатов прогона MRP
// - Сводка прогона (summary)
// - Справочники (items/resources) через useDictionaries
// - Уведомления об ошибках через Quasar Notify

import { ref } from 'vue'
import { defineStore } from 'pinia'
import { Notify } from 'quasar'
import { getPlanningRunSummary } from '../services/api'
import { useDictionaries } from '../composables/useDictionaries'
import type { MRPSummary } from '../types/mrp'

export const useMRPResultsStore = defineStore('mrpResults', () => {
  const runId = ref<number | null>(null)

  // Summary
  const summary = ref<MRPSummary | null>(null)
  const loadingSummary = ref(false)

  // Справочники (items/resources)
  const {
    itemMap,
    areaMap,
    loadingItems,
    loadingAreas,
    loadedItems,
    loadedAreas,
    loadingAny,
    ensureLoaded,
    fillMissingFromRows
  } = useDictionaries()

  function setRunId(id: number) {
    runId.value = id
  }

  async function loadSummary() {
    if (!runId.value) return
    loadingSummary.value = true
    try {
      const data = await getPlanningRunSummary(runId.value)
      summary.value = data as MRPSummary
    } catch (e: any) {
      Notify.create({
        type: 'negative',
        message: 'Ошибка загрузки сводки прогона',
        caption: e?.message || String(e)
      })
    } finally {
      loadingSummary.value = false
    }
  }

  async function ensureDictionaries() {
    try {
      await ensureLoaded()
    } catch (e: any) {
      // Частичные ошибки уже подавлены в composable, но продублируем уведомление
      Notify.create({
        type: 'warning',
        message: 'Не удалось загрузить часть справочников',
        caption: e?.message || String(e)
      })
    }
  }

  function reset() {
    summary.value = null
    runId.value = null
  }

  return {
    // state
    runId,
    summary,
    loadingSummary,
    // dictionaries
    itemMap,
    areaMap,
    loadingItems,
    loadingAreas,
    loadedItems,
    loadedAreas,
    loadingAny,
    // actions
    setRunId,
    loadSummary,
    ensureDictionaries,
    fillMissingFromRows,
    reset
  }
})

export default useMRPResultsStore