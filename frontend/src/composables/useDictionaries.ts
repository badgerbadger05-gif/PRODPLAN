import { ref, computed } from 'vue'
import api, { listItems, listResources } from '../services/api'
import type { ItemInfo, ResourceInfo, ItemMap, AreaMap } from '../types/mrp'

type FillOptions = {
  concurrency?: number
  maxIds?: number
}

function toSafeItem(row: any): ItemInfo {
  return {
    item_id: Number(row.item_id),
    item_name: row.item_name ?? null,
    item_article: row.item_article ?? null,
    unit: row.unit ?? null
  }
}

function toSafeResource(row: any): ResourceInfo {
  return {
    resource_id: Number(row.resource_id),
    resource_name: String(row.resource_name ?? '')
  }
}

async function runWithConcurrency<T>(factories: Array<() => Promise<T>>, concurrency = 5): Promise<T[]> {
  const results: T[] = []
  let idx = 0
  const workers: Promise<void>[] = []
  const work = async () => {
    while (true) {
      const myIndex = idx++
      if (myIndex >= factories.length) break
      const fn = factories[myIndex] as (() => Promise<T>)
      const res = await fn()
      results[myIndex] = res
    }
  }
  const pool = Math.max(1, Math.min(concurrency, factories.length || 1))
  for (let i = 0; i < pool; i++) workers.push(work())
  await Promise.all(workers)
  return results
}

/**
 * Компоузабл для загрузки и кэширования справочников (items/resources) с защитой от N+1.
 * - Первичная массовая загрузка listItems/listResources
 * - Догрузка недостающих id с ограничением конкурентности
 */
export function useDictionaries() {
  const itemMap = ref<ItemMap>({})
  const areaMap = ref<AreaMap>({})
  const loadingItems = ref(false)
  const loadingAreas = ref(false)
  const loadedItems = ref(false)
  const loadedAreas = ref(false)
  const lastError = ref<unknown | null>(null)

  const hasItems = computed(() => Object.keys(itemMap.value).length > 0)
  const hasAreas = computed(() => Object.keys(areaMap.value).length > 0)
  const loadingAny = computed(() => loadingItems.value || loadingAreas.value)

  function upsertItems(rows: any[]) {
    const map = { ...itemMap.value }
    for (const r of rows || []) {
      if (r?.item_id == null) continue
      const it = toSafeItem(r)
      map[it.item_id] = { ...(map[it.item_id] || {}), ...it }
    }
    itemMap.value = map
  }

  function upsertAreas(rows: any[]) {
    const map = { ...areaMap.value }
    for (const r of rows || []) {
      if (r?.resource_id == null) continue
      const res = toSafeResource(r)
      map[res.resource_id] = res.resource_name
    }
    areaMap.value = map
  }

  async function loadItemsAll(limit = 10000) {
    loadingItems.value = true
    lastError.value = null
    try {
      const resp = await listItems({ limit, offset: 0 })
      upsertItems(resp.rows || [])
      loadedItems.value = true
    } catch (e) {
      lastError.value = e
      // не бросаем исключение: даём шанс частично работать
      // console.error('useDictionaries.loadItemsAll failed', e)
    } finally {
      loadingItems.value = false
    }
  }

  async function loadAreasAll(limit = 1000) {
    loadingAreas.value = true
    lastError.value = null
    try {
      const resp = await listResources({ limit, offset: 0 })
      upsertAreas(resp.rows || [])
      loadedAreas.value = true
    } catch (e) {
      lastError.value = e
      // console.error('useDictionaries.loadAreasAll failed', e)
    } finally {
      loadingAreas.value = false
    }
  }

  async function ensureLoaded() {
    // Параллельная первичная загрузка
    await Promise.all([
      loadedItems.value ? Promise.resolve() : loadItemsAll(),
      loadedAreas.value ? Promise.resolve() : loadAreasAll()
    ])
  }

  function findMissingItemIds(ids: Array<number | null | undefined>): number[] {
    const set = new Set<number>()
    for (const raw of ids) {
      const id = Number(raw)
      if (!id || Number.isNaN(id)) continue
      if (!itemMap.value[id]) set.add(id)
    }
    return Array.from(set)
  }

  function findMissingAreaIds(ids: Array<number | null | undefined>): number[] {
    const set = new Set<number>()
    for (const raw of ids) {
      const id = Number(raw)
      if (!id || Number.isNaN(id)) continue
      if (!areaMap.value[id]) set.add(id)
    }
    return Array.from(set)
  }

  async function fillMissingItemsByIds(allIds: number[], opts: FillOptions = {}) {
    const maxIds = opts.maxIds ?? 500
    const concurrency = opts.concurrency ?? 5
    const ids = Array.from(new Set(allIds)).filter(id => id > 0).slice(0, maxIds)
    if (ids.length === 0) return

    // Создаём фабрики задач с axios GET /v1/items/{id}
    const tasks = ids.map(id => async () => {
      try {
        const resp = await api.get(`/v1/items/${id}`)
        const data = resp?.data
        if (data && data.item_id != null) {
          upsertItems([data])
        }
      } catch {
        // глушим частичные ошибки
      }
    })
    await runWithConcurrency(tasks, concurrency)
  }

  async function fillMissingAreasByIds(allIds: number[], opts: FillOptions = {}) {
    const maxIds = opts.maxIds ?? 200
    const concurrency = opts.concurrency ?? 5
    const ids = Array.from(new Set(allIds)).filter(id => id > 0).slice(0, maxIds)
    if (ids.length === 0) return

    const tasks = ids.map(id => async () => {
      try {
        const resp = await api.get(`/v1/resources/${id}`)
        const data = resp?.data
        if (data && data.resource_id != null) {
          upsertAreas([data])
        }
      } catch {
        // глушим частичные ошибки
      }
    })
    await runWithConcurrency(tasks, concurrency)
  }

  /**
   * Догрузка недостающих справочников по фактическим строкам.
   * Ожидает массив объектов, где встречаются поля item_id и/или stages[].area_id
   */
  async function fillMissingFromRows(rows: any[], opts?: { items?: FillOptions; areas?: FillOptions }) {
    try {
      const itemIds: number[] = []
      const areaIds: number[] = []
      for (const r of rows || []) {
        if (r?.item_id != null) itemIds.push(Number(r.item_id))
        // Возможная структура stages
        const stages = Array.isArray(r?.stages) ? r.stages : []
        for (const s of stages) {
          if (s?.area_id != null) areaIds.push(Number(s.area_id))
        }
      }
      const missItems = findMissingItemIds(itemIds)
      const missAreas = findMissingAreaIds(areaIds)
      await Promise.all([
        missItems.length ? fillMissingItemsByIds(missItems, opts?.items) : Promise.resolve(),
        missAreas.length ? fillMissingAreasByIds(missAreas, opts?.areas) : Promise.resolve()
      ])
    } catch (e) {
      lastError.value = e
      // console.error('useDictionaries.fillMissingFromRows failed', e)
    }
  }

  return {
    // state
    itemMap,
    areaMap,
    loadingItems,
    loadingAreas,
    loadedItems,
    loadedAreas,
    loadingAny,
    lastError,
    hasItems,
    hasAreas,
    // actions
    ensureLoaded,
    loadItemsAll,
    loadAreasAll,
    upsertItems,
    upsertAreas,
    findMissingItemIds,
    findMissingAreaIds,
    fillMissingItemsByIds,
    fillMissingAreasByIds,
    fillMissingFromRows
  }
}

export default useDictionaries