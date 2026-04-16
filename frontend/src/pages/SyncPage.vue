<template>
  <q-page class="q-pa-lg">
    <div class="row justify-center">
      <div class="col-12 col-md-10 col-lg-8">
        <q-card>
          <q-card-section>
            <div class="text-h5">Настройки 1С OData</div>
            <div class="text-subtitle2 text-grey-7">Аналог настроек из NiceGUI</div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <div class="row q-col-gutter-md">
              <div class="col-12">
                <q-input v-model="form.base_url" label="Базовый URL (base_url)" dense filled placeholder="http://srv-1c:8080/base/odata/standard.odata" />
              </div>
              <div class="col-12 col-md-4">
                <q-input v-model="form.username" label="Имя пользователя (username)" dense filled />
              </div>
              <div class="col-12 col-md-4">
                <q-input v-model="form.password" label="Пароль (password)" type="password" dense filled />
              </div>
              <div class="col-12 col-md-4">
                <q-input v-model="form.token" label="Bearer токен (опционально)" dense filled />
              </div>
            </div>

            <div class="row q-col-gutter-sm q-mt-md">
              <div class="col-auto">
                <q-btn color="primary" label="Сохранить настройки" @click="saveConfig" :loading="loading.save" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Тест подключения ($metadata)" @click="testConn" :loading="loading.test" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Выгрузить метаданные" @click="fetchMetadata" :loading="loading.meta" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Выгрузить группы номенклатуры" @click="exportGroups" :loading="loading.groups" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Синхронизация спецификаций" @click="syncSpecifications" :loading="loading.syncSpecifications" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Синхронизация операций" @click="syncOperations" :loading="loading.syncOperations" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Синхронизация остатков" @click="syncStock" :loading="loading.syncStock" />
              </div>
              <div class="col-auto">
                <q-btn color="secondary" label="Синхронизация складов" @click="syncWarehouses" :loading="loading.syncWarehouses" />
              </div>
              <div class="col-auto">
                <q-btn color="primary" label="Синхронизация номенклатуры + ЕИ" @click="syncNomenclature" :loading="loading.syncNomenclature" />
              </div>
              <div class="col-auto">
                <q-btn color="primary" label="Синхронизация видов производства" @click="syncProductionKinds" :loading="loading.syncProductionKinds" />
              </div>
              <div class="col-auto">
                <q-btn color="primary" label="Синхронизация заказов на производство" @click="syncProductionOrders" :loading="loading.syncProductionOrders" />
              </div>
              <div class="col-auto">
                <q-btn dense flat color="secondary" icon="table_view" @click="exportProductionOrders" :loading="loading.exportProductionOrders">
                  <q-tooltip>Экспорт заказов в Excel</q-tooltip>
                </q-btn>
              </div>
            </div>

            <!-- Прогресс-бар синхронизации номенклатуры -->
            <div v-if="syncProgress.show" class="q-mt-md">
              <div class="text-subtitle2 q-mb-sm">Прогресс синхронизации</div>
              <q-linear-progress :value="syncProgress.value" :indeterminate="syncProgress.value <= 0" color="primary" size="20px">
                <div class="absolute-full flex flex-center">
                  <q-badge color="white" text-color="primary" :label="syncProgress.label" />
                </div>
              </q-linear-progress>
              <div v-if="syncProgress.details" class="text-caption text-grey-7 q-mt-xs">
                {{ syncProgress.details }}
              </div>
            </div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <div class="text-subtitle1 q-mb-sm">Склады для учёта остатков в расчёте</div>
            <div class="row q-col-gutter-sm q-mb-sm">
              <div class="col-auto">
                <q-btn outline label="Обновить список складов" @click="loadWarehouses" :loading="loading.loadWarehouses" />
              </div>
              <div class="col-auto">
                <q-btn outline label="Выбрать все" @click="selectAllWarehouses" />
              </div>
              <div class="col-auto">
                <q-btn outline label="Снять все" @click="clearAllWarehouses" />
              </div>
              <div class="col-auto">
                <q-btn color="primary" label="Сохранить выбор складов" @click="saveWarehouseSelection" :loading="loading.saveWarehousesSel" />
              </div>
              <div class="col-12 text-caption text-grey-7">
                Всего складов: {{ warehouses.length }} • Выбрано: {{ selectedWarehouseRefs.size }}
              </div>
            </div>

            <div class="q-pa-sm groups-box q-mb-md">
              <q-list dense v-if="warehouses.length">
                <q-item v-for="w in warehouses" :key="w.warehouse_ref1c" tag="label">
                  <q-item-section avatar>
                    <q-checkbox :model-value="selectedWarehouseRefs.has(w.warehouse_ref1c)" @update:model-value="(v:boolean)=>toggleWarehouseSel(w.warehouse_ref1c,v)" />
                  </q-item-section>
                  <q-item-section>
                    <q-item-label>{{ w.warehouse_code ? (w.warehouse_code + ' — ') : '' }}{{ w.warehouse_name }}</q-item-label>
                  </q-item-section>
                </q-item>
              </q-list>
              <div v-else class="text-grey-6">Список складов пуст. Нажмите «Синхронизация складов» выше.</div>
            </div>

            <div class="text-subtitle1 q-mb-sm">Группы номенклатуры (IsFolder=true)</div>
            <div class="row q-col-gutter-sm q-mb-sm">
              <div class="col-auto">
                <q-btn outline label="Обновить список" @click="loadGroups" :loading="loading.loadGroups" />
              </div>
              <div class="col-auto">
                <q-btn outline label="Выбрать все" @click="selectAll" />
              </div>
              <div class="col-auto">
                <q-btn outline label="Снять все" @click="clearAll" />
              </div>
              <div class="col-auto">
                <q-btn color="primary" label="Сохранить выбор" @click="saveSelection" :loading="loading.saveSel" />
              </div>
              <div class="col-12 text-caption text-grey-7">
                Всего групп: {{ groups.length }} • Выбрано: {{ selectedIds.size }}
              </div>
            </div>

            <div class="q-pa-sm groups-box">
              <q-list dense v-if="groups.length">
                <q-item v-for="g in groups" :key="g.id" tag="label">
                  <q-item-section avatar>
                    <q-checkbox :model-value="selectedIds.has(g.id)" @update:model-value="(v:boolean)=>toggleSel(g.id,v)" />
                  </q-item-section>
                  <q-item-section>
                    <q-item-label>{{ g.code }} — {{ g.name }}</q-item-label>
                  </q-item-section>
                </q-item>
              </q-list>
              <div v-else class="text-grey-6">Список групп пуст. Нажмите «Обновить список» или «Выгрузить группы номенклатуры» выше.</div>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount, onMounted } from 'vue'
import { Notify } from 'quasar'
import api from '../services/api'

type ODataConfig = {
  base_url: string
  username?: string
  password?: string
  token?: string
}

type GroupItem = { id: string; code: string; name: string }
type WarehouseItem = {
  warehouse_id: number
  warehouse_ref1c: string
  warehouse_code: string
  warehouse_name: string
  is_selected: boolean
}

const form = ref<ODataConfig>({
  base_url: '',
  username: '',
  password: '',
  token: ''
})

const groups = ref<GroupItem[]>([])
const selectedIds = ref<Set<string>>(new Set())
const warehouses = ref<WarehouseItem[]>([])
const selectedWarehouseRefs = ref<Set<string>>(new Set())

const syncProgress = ref({
  show: false,
  value: 0,
  label: '0%',
  details: ''
})

const loading = ref({
  save: false,
  test: false,
  meta: false,
  groups: false,
  loadGroups: false,
  saveSel: false,
  syncNomenclature: false,
  syncSpecifications: false,
  syncOperations: false,
  syncStock: false,
  syncWarehouses: false,
  syncProductionKinds: false,
  syncProductionOrders: false,
  exportProductionOrders: false,
  loadWarehouses: false,
  saveWarehousesSel: false
})

// --- Реал-тайм прогресс синхронизации номенклатуры ---
const progressTimer = ref<number | null>(null)
const progressKey = ref<'nomenclature' | 'units' | 'operations' | 'stock'>('nomenclature')

async function pollProgress() {
  try {
    const { data } = await api.get('/v1/sync/progress', { params: { key: progressKey.value } })
    const total = Number(data?.total || 0)
    const processed = Number(data?.processed || 0)
    const percent = Math.max(0, Math.min(1, Number(data?.percent || 0)))
    syncProgress.value.value = percent
    syncProgress.value.label = `${Math.round(percent * 100)}%`
    const message = data?.message ? String(data.message) : ''
    syncProgress.value.details = `${processed}${total ? ' / ' + total : ''}${message ? ' • ' + message : ''}`
    if (data?.finished) {
      stopProgressPolling()
    }
  } catch {
    // игнорируем ошибки опроса, чтобы не мешать UI
  }
}

function startProgressPolling() {
  stopProgressPolling()
  // мгновенно запросим состояние
  void pollProgress()
  progressTimer.value = window.setInterval(pollProgress, 1000)
}

function stopProgressPolling() {
  if (progressTimer.value != null) {
    clearInterval(progressTimer.value)
    progressTimer.value = null
  }
}

onBeforeUnmount(() => {
  stopProgressPolling()
})

async function loadConfig() {
  try {
    const { data } = await api.get('/v1/odata/config')
    const cfg = data || {}
    form.value.base_url = cfg.base_url || ''
    form.value.username = cfg.username || ''
    form.value.password = cfg.password || ''
    form.value.token = cfg.token || ''
  } catch {
    // ignore
  }
}

async function loadWarehouses() {
  try {
    loading.value.loadWarehouses = true
    const { data } = await api.get('/v1/sync/warehouses')
    const rows = Array.isArray(data?.rows) ? data.rows : []
    warehouses.value = rows.map((r:any) => ({
      warehouse_id: Number(r.warehouse_id || 0),
      warehouse_ref1c: String(r.warehouse_ref1c || ''),
      warehouse_code: String(r.warehouse_code || ''),
      warehouse_name: String(r.warehouse_name || ''),
      is_selected: Boolean(r.is_selected)
    }))
    selectedWarehouseRefs.value = new Set(
      warehouses.value.filter(w => w.is_selected).map(w => w.warehouse_ref1c)
    )
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка загрузки складов'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.loadWarehouses = false
  }
}

function toggleWarehouseSel(ref: string, selected: boolean) {
  if (!ref) return
  const set = selectedWarehouseRefs.value
  if (selected) set.add(ref)
  else set.delete(ref)
}

function selectAllWarehouses() {
  selectedWarehouseRefs.value = new Set(warehouses.value.map(w => w.warehouse_ref1c))
}

function clearAllWarehouses() {
  selectedWarehouseRefs.value = new Set()
}

async function saveWarehouseSelection() {
  try {
    loading.value.saveWarehousesSel = true
    await api.post('/v1/sync/warehouses/selection', {
      selected_refs: Array.from(selectedWarehouseRefs.value)
    })
    Notify.create({ type: 'positive', message: 'Выбор складов сохранён' })
    await loadWarehouses()
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка сохранения выбора складов'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.saveWarehousesSel = false
  }
}

async function saveConfig() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url' })
    return
  }
  try {
    loading.value.save = true
    await api.post('/v1/odata/config', form.value)
    Notify.create({ type: 'positive', message: 'Настройки сохранены' })
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка сохранения настроек'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.save = false
  }
}

async function testConn() {
  try {
    loading.value.test = true
    const { data } = await api.post('/v1/odata/test', form.value)
    Notify.create({ type: 'positive', message: `Подключение OK • ${data.bytes} bytes (${data.type})` })
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка теста подключения'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.test = false
  }
}

async function fetchMetadata() {
  try {
    loading.value.meta = true
    const { data } = await api.post('/v1/odata/metadata', form.value)
    Notify.create({ type: 'positive', message: `Метаданные выгружены • EntitySets: ${data.entity_sets}` })
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка выгрузки метаданных'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.meta = false
  }
}

async function exportGroups() {
  try {
    loading.value.groups = true
    const { data } = await api.post('/v1/odata/categories/export_groups', form.value)
    Notify.create({ type: 'positive', message: `Выгружено групп: ${data.total}` })
    await loadGroups()
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка выгрузки групп'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.groups = false
  }
}

async function loadSelection() {
  try {
    const sel = await api.get('/v1/odata/groups/selection')
    const ids: string[] = Array.isArray(sel.data?.ids) ? sel.data.ids : []
    selectedIds.value = new Set(ids)
  } catch {
    // не сбрасываем локальное состояние при временной ошибке загрузки выбора
  }
}

async function loadGroups() {
  try {
    loading.value.loadGroups = true
    const { data } = await api.get('/v1/odata/groups')
    const raw = Array.isArray(data?.value) ? data.value : []
    groups.value = raw
      .filter((r:any)=>r && (r.Ref_Key || r.id))
      .map((r:any)=>({
        id: String(r.Ref_Key || r.id),
        code: String(r.Code || r.code || ''),
        name: String(r.Description || r.name || '')
      }))
      .sort((a:GroupItem,b:GroupItem)=> (a.code+a.name).localeCompare(b.code+b.name))
    await loadSelection()
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка загрузки групп'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.loadGroups = false
  }
}

function toggleSel(id: string, v: boolean) {
  const set = selectedIds.value
  if (v) set.add(id)
  else set.delete(id)
}

function selectAll() {
  const set = new Set<string>()
  for (const g of groups.value) set.add(g.id)
  selectedIds.value = set
}

function clearAll() {
  selectedIds.value = new Set()
}

async function saveSelection() {
  try {
    loading.value.saveSel = true
    await api.post('/v1/odata/groups/selection', { ids: Array.from(selectedIds.value) })
    Notify.create({ type: 'positive', message: 'Выбор групп сохранён' })
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка сохранения выбора'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.saveSel = false
  }
}

async function syncSpecifications() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }

  try {
    loading.value.syncSpecifications = true

    // Показ локального прогресса (без опроса, т.к. бэкенд для спецификаций/этапов не шлёт прогресс)
    syncProgress.value.show = true
    syncProgress.value.value = 0
    syncProgress.value.label = '0%'
    syncProgress.value.details = 'Старт синхронизации этапов...'

    const basePayload = {
      base_url: form.value.base_url,
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: null,
      select_fields: null,
      dry_run: false,
      zero_missing: false
    }

    // Шаг 1 — этапы производства (Catalog_ЭтапыПроизводства)
    const stagePayload = { ...basePayload, entity_name: 'Catalog_ЭтапыПроизводства' }
    const { data: stageData } = await api.post('/v1/sync/production-stages-odata', stagePayload, { timeout: 900000 })
    const createdStages = Number(stageData?.stages_created || 0)
    const updatedStages = Number(stageData?.stages_updated || 0)

    syncProgress.value.value = 0.33
    syncProgress.value.label = '33%'
    syncProgress.value.details = `Этапы: создано ${createdStages}, обновлено ${updatedStages}`

    // Шаг 2 — спецификации (состав и операции)
    const specPayload = { ...basePayload, entity_name: 'Catalog_Спецификации' }
    const { data: specsData } = await api.post('/v1/sync/specifications-odata', specPayload, { timeout: 900000 })
    const createdSpecs = Number(specsData?.specs_created || 0)
    const updatedSpecs = Number(specsData?.specs_updated || 0)
    const createdComps = Number(specsData?.components_created || 0)
    const updatedComps = Number(specsData?.components_updated || 0)

    syncProgress.value.value = 0.66
    syncProgress.value.label = '66%'
    syncProgress.value.details = `Спецификации: создано ${createdSpecs}, обновлено ${updatedSpecs} • Состав: создано ${createdComps}, обновлено ${updatedComps}`

    // Шаг 3 — спецификации по умолчанию
    const defSpecPayload = { ...basePayload, entity_name: 'InformationRegister_СпецификацииПоУмолчанию' }
    const { data: defData } = await api.post('/v1/sync/default-specifications-odata', defSpecPayload, { timeout: 900000 })
    const createdRecs = Number(defData?.records_created || 0)
    const updatedRecs = Number(defData?.records_updated || 0)

    syncProgress.value.value = 1
    syncProgress.value.label = '100%'
    syncProgress.value.details = `Спецификации по умолчанию: создано ${createdRecs}, обновлено ${updatedRecs}`

    Notify.create({
      type: 'positive',
      message: `Синхронизация завершена • Этапы: ${createdStages}/${updatedStages} • Спеки: ${createdSpecs}/${updatedSpecs} • Состав+: ${createdComps}/${updatedComps} • По умолчанию+: ${createdRecs}/${updatedRecs}`,
      timeout: 6000
    })

    setTimeout(() => {
      syncProgress.value.show = false
    }, 2500)
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации спецификаций/этапов'
    Notify.create({ type: 'negative', message: String(msg) })
    syncProgress.value.show = false
  } finally {
    loading.value.syncSpecifications = false
  }
}

async function syncNomenclature() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }

  try {
    loading.value.syncNomenclature = true
    syncProgress.value.show = true
    syncProgress.value.value = 0
    syncProgress.value.label = '0%'
    syncProgress.value.details = 'Старт синхронизации номенклатуры...'

    // Шаг 1 — Номенклатура (с прогрессом по ключу 'nomenclature')
    progressKey.value = 'nomenclature'
    startProgressPolling()

    const payload = {
      base_url: form.value.base_url,
      entity_name: 'Catalog_Номенклатура',
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: null,
      select_fields: null,
      dry_run: false,
      zero_missing: false
    }

    const { data: nomData } = await api.post('/v1/sync/nomenclature-odata', payload)

    // Финализируем прогресс номенклатуры и переходим к ЕИ
    await pollProgress()
    stopProgressPolling()
    const nomCreated = Number(nomData?.items_created || 0)
    const nomUpdated = Number(nomData?.items_updated || 0)
    const nomCatsCreated = Number(nomData?.categories_created || 0)
    syncProgress.value.value = 0.5
    syncProgress.value.label = '50%'
    syncProgress.value.details = `Номенклатура: создано ${nomCreated}, обновлено ${nomUpdated}, категорий ${nomCatsCreated}. Старт синхронизации единиц...`

    // Шаг 2 — Единицы измерения (с прогрессом по ключу 'units')
    try {
      progressKey.value = 'units'
      startProgressPolling()

      const unitsPayload = {
        base_url: form.value.base_url,
        entity_name: 'Catalog_ЕдиницыИзмерения',
        username: form.value.username || undefined,
        password: form.value.password || undefined,
        token: form.value.token || undefined,
        filter_query: null,
        select_fields: null,
        dry_run: false,
        zero_missing: false
      }

      const { data: unitsData } = await api.post('/v1/sync/units-odata', unitsPayload, { timeout: 900000 })

      await pollProgress()
      stopProgressPolling()

      const unitsCreated = Number(unitsData?.units_created || 0)
      const unitsUpdated = Number(unitsData?.units_updated || 0)

      syncProgress.value.value = 1
      syncProgress.value.label = '100%'
      syncProgress.value.details = `Номенклатура: создано ${nomCreated}, обновлено ${nomUpdated}. Единицы: создано ${unitsCreated}, обновлено ${unitsUpdated}`

      Notify.create({
        type: 'positive',
        message: `Номенклатура OK (${nomCreated}/${nomUpdated}); Единицы OK (${unitsCreated}/${unitsUpdated})`,
        timeout: 6000
      })
    } catch (e:any) {
      // Ошибка только на шаге ЕИ — не сваливаем общий флоу
      stopProgressPolling()
      const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации единиц измерения'
      Notify.create({ type: 'negative', message: `Единицы измерения: ${String(msg)}` })
      syncProgress.value.value = 1
      syncProgress.value.label = '100%'
      syncProgress.value.details = `Номенклатура выполнена. Единицы: ошибка — ${String(msg)}`
    }

    // Скрываем прогресс через 3 секунды
    setTimeout(() => {
      syncProgress.value.show = false
    }, 3000)

  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации номенклатуры'
    Notify.create({ type: 'negative', message: String(msg) })
    stopProgressPolling()
    syncProgress.value.show = false
  } finally {
    loading.value.syncNomenclature = false
  }
}

async function syncOperations() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }
  try {
    loading.value.syncOperations = true

    // Покажем общий прогресс (опрашиваем /v1/sync/progress с ключом 'operations')
    syncProgress.value.show = true
    syncProgress.value.value = 0
    syncProgress.value.label = '0%'
    syncProgress.value.details = 'Старт синхронизации операций...'

    progressKey.value = 'operations'
    startProgressPolling()

    const payload = {
      base_url: form.value.base_url,
      entity_name: 'Catalog_Спецификации_Операции',
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: null,
      select_fields: null,
      dry_run: false,
      zero_missing: false
    }

    const { data } = await api.post('/v1/sync/operations-odata', payload, { timeout: 900000 })

    // Финализируем прогресс
    await pollProgress()
    stopProgressPolling()

    const created = Number(data?.operations_created || 0)
    const updated = Number(data?.operations_updated || 0)
    const seen = Number(data?.operations_seen_unique || 0)

    syncProgress.value.value = 1
    syncProgress.value.label = '100%'
    syncProgress.value.details = `Операции: уникальных ${seen}, создано ${created}, обновлено ${updated}`

    Notify.create({
      type: 'positive',
      message: `Операции синхронизированы • уникальных ${seen}, создано ${created}, обновлено ${updated}`,
      timeout: 6000
    })

    setTimeout(() => {
      syncProgress.value.show = false
    }, 2500)
  } catch (e:any) {
    stopProgressPolling()
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации операций'
    Notify.create({ type: 'negative', message: String(msg) })
    syncProgress.value.show = false
  } finally {
    loading.value.syncOperations = false
  }
}

async function syncStock() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }
  try {
    loading.value.syncStock = true

    // Включаем реальный прогресс через /v1/sync/progress с ключом 'stock'
    syncProgress.value.show = true
    syncProgress.value.value = 0
    syncProgress.value.label = '0%'
    syncProgress.value.details = 'Старт синхронизации остатков...'

    progressKey.value = 'stock'
    startProgressPolling()

    // Подготовим фильтр по текущему моменту для регистра накопления Balance
    const now = new Date()
    const pad = (n:number) => String(n).padStart(2, '0')
    const dt = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
    const payload = {
      base_url: form.value.base_url,
      entity_name: 'AccumulationRegister_ЗапасыНаСкладах/Balance',
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: `Period le datetime'${dt}'`,
      select_fields: null,
      dry_run: false,
      // IMPORTANT: many 1C Balance endpoints return only non-zero rows.
      // Missing items must be treated as zero, otherwise old остатки stay in DB and break MRP.
      zero_missing: true
    }

    const { data } = await api.post('/v1/sync/stock-odata', payload, { timeout: 900000 })

    // Финализируем прогресс: один явный запрос и стоп
    await pollProgress()
    stopProgressPolling()

    const itemsTotal = Number(data?.items_total ?? 0)
    const matched = Number(data?.matched_in_odata ?? 0)
    const updated = Number(data?.items_updated ?? 0)
    const unchanged = Number(data?.items_unchanged ?? 0)
    const zeroed = Number(data?.unmatched_zeroed ?? 0)
    const dryRun = Boolean(data?.dry_run ?? false)

    // Отобразим итог
    syncProgress.value.value = 1
    syncProgress.value.label = '100%'
    syncProgress.value.details = `Всего ${itemsTotal}, совпало ${matched}, обновлено ${updated}, без изменений ${unchanged}${zeroed ? ', обнулено ' + zeroed : ''}${dryRun ? ' (dry-run)' : ''}`

    Notify.create({
      type: 'positive',
      message: `Остатки синхронизированы • всего ${itemsTotal}, совпало ${matched}, обновлено ${updated}, без изменений ${unchanged}${zeroed ? ', обнулено ' + zeroed : ''}${dryRun ? ' (dry-run)' : ''}`,
      timeout: 6000
    })

    setTimeout(() => {
      syncProgress.value.show = false
    }, 2500)
  } catch (e:any) {
    stopProgressPolling()
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации остатков'
    Notify.create({ type: 'negative', message: String(msg) })
    syncProgress.value.show = false
  } finally {
    loading.value.syncStock = false
  }
}

async function syncWarehouses() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }
  try {
    loading.value.syncWarehouses = true

    const now = new Date()
    const pad = (n:number) => String(n).padStart(2, '0')
    const dt = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
    const payload = {
      base_url: form.value.base_url,
      entity_name: 'AccumulationRegister_ЗапасыНаСкладах/Balance',
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: `Period le datetime'${dt}'`,
      select_fields: null,
      dry_run: false,
      zero_missing: false
    }

    const { data } = await api.post('/v1/sync/warehouses-odata', payload, { timeout: 900000 })
    const total = Number(data?.warehouses_total || 0)
    const selected = Number(data?.warehouses_selected || 0)
    const changed = Number(data?.warehouses_changed || 0)
    Notify.create({
      type: 'positive',
      message: `Склады синхронизированы • всего ${total}, выбрано ${selected}, изменено ${changed}`,
      timeout: 5000
    })
    await loadWarehouses()
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации складов'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.syncWarehouses = false
  }
}

async function syncProductionKinds() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }

  try {
    loading.value.syncProductionKinds = true

    // Покажем общий прогресс (опрашиваем /v1/sync/progress с ключом 'production_kinds', если поддерживается)
    syncProgress.value.show = true
    syncProgress.value.value = 0
    syncProgress.value.label = '0%'
    syncProgress.value.details = 'Старт синхронизации видов производства...'

    const payload = {
      base_url: form.value.base_url,
      entity_name: 'Catalog_ВидыПроизводства',
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: null,
      select_fields: null,
      dry_run: false,
      zero_missing: false
    }

    const { data } = await api.post('/v1/sync/production-kinds-odata', payload, { timeout: 900000 })

    const created = Number(data?.kinds_created || 0)
    const updated = Number(data?.kinds_updated || 0)
    const total = Number(data?.kinds_total || 0)

    syncProgress.value.value = 1
    syncProgress.value.label = '100%'
    syncProgress.value.details = `Виды производства: всего ${total}, создано ${created}, обновлено ${updated}`

    Notify.create({
      type: 'positive',
      message: `Виды производства синхронизированы • всего ${total}, создано ${created}, обновлено ${updated}`,
      timeout: 6000
    })

    setTimeout(() => {
      syncProgress.value.show = false
    }, 2500)
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации видов производства'
    Notify.create({ type: 'negative', message: String(msg) })
    syncProgress.value.show = false
  } finally {
    loading.value.syncProductionKinds = false
  }
}

async function syncProductionOrders() {
  if (!form.value.base_url) {
    Notify.create({ type: 'warning', message: 'Укажите base_url для подключения к 1С' })
    return
  }

  try {
    loading.value.syncProductionOrders = true
    syncProgress.value.show = true
    syncProgress.value.value = 0
    syncProgress.value.label = '0%'
    syncProgress.value.details = 'Старт синхронизации производственных заказов...'

    const payload = {
      base_url: form.value.base_url,
      entity_name: 'Document_ЗаказНаПроизводство',
      username: form.value.username || undefined,
      password: form.value.password || undefined,
      token: form.value.token || undefined,
      filter_query: null,
      select_fields: null,
      dry_run: false,
      zero_missing: false
    }

    const { data } = await api.post('/v1/sync/production-orders-odata', payload, { timeout: 900000 })

    const total = Number(data?.orders_total || 0)
    const created = Number(data?.orders_created || 0)
    const updated = Number(data?.orders_updated || 0)
    const unchanged = Number(data?.orders_unchanged || 0)
    const prodCreated = Number(data?.products_created || 0)
    const prodUpdated = Number(data?.products_updated || 0)

    syncProgress.value.value = 1
    syncProgress.value.label = '100%'
    syncProgress.value.details = `Заказы: всего ${total}, создано ${created}, обновлено ${updated}, без изменений ${unchanged} • Строки: создано ${prodCreated}, обновлено ${prodUpdated}`

    Notify.create({
      type: 'positive',
      message: `Заказы на производство синхронизированы • всего ${total}, создано ${created}, обновлено ${updated}`,
      timeout: 6000
    })

    setTimeout(() => {
      syncProgress.value.show = false
    }, 2500)
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка синхронизации заказов на производство'
    Notify.create({ type: 'negative', message: String(msg) })
    syncProgress.value.show = false
  } finally {
    loading.value.syncProductionOrders = false
  }
}

/**
 * Экспорт заказов на производство в Excel (XLSX)
 * Данные берутся из БД
 */
async function exportProductionOrders() {
  try {
    loading.value.exportProductionOrders = true
    
    const { data } = await api.get('/v1/sync/production-orders-odata/export', {
      timeout: 60000
    })
    
    // Скачиваем файл
    if (data?.data_base64) {
      downloadBase64Xlsx(data.data_base64, data.filename || 'production_orders.xlsx')
      
      Notify.create({
        type: 'positive',
        message: `Экспорт выполнен • строк: ${data.total_rows || 0}, заказов: ${data.orders_count || 0}`,
        timeout: 3000
      })
    }
  } catch (e: any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка экспорта заказов'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.exportProductionOrders = false
  }
}

/**
 * Скачивание base64 XLSX файла
 */
function downloadBase64Xlsx(b64: string, filename: string) {
  try {
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
  } catch (e) {
    console.error('Download XLSX failed', e)
    Notify.create({ type: 'negative', message: 'Ошибка скачивания файла' })
  }
}

onMounted(() => {
  void loadConfig()
  void loadGroups()
  void loadWarehouses()
})
  
</script>

<style scoped>
.groups-box {
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  max-height: 360px;
  overflow: auto;
}
</style>
