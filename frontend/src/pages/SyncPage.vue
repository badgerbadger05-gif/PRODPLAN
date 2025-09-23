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
                <q-btn color="primary" label="Синхронизация номенклатуры" @click="syncNomenclature" :loading="loading.syncNomenclature" />
              </div>
            </div>

            <!-- Прогресс-бар синхронизации номенклатуры -->
            <div v-if="syncProgress.show" class="q-mt-md">
              <div class="text-subtitle2 q-mb-sm">Прогресс синхронизации</div>
              <q-linear-progress :value="syncProgress.value" color="primary" size="20px">
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
import { ref, onBeforeUnmount } from 'vue'
import { Notify } from 'quasar'
import api from '../services/api'

type ODataConfig = {
  base_url: string
  username?: string
  password?: string
  token?: string
}

type GroupItem = { id: string; code: string; name: string }

const form = ref<ODataConfig>({
  base_url: '',
  username: '',
  password: '',
  token: ''
})

const groups = ref<GroupItem[]>([])
const selectedIds = ref<Set<string>>(new Set())

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
  syncOperations: false
})

// --- Реал-тайм прогресс синхронизации номенклатуры ---
const progressTimer = ref<number | null>(null)
const progressKey = ref<'nomenclature' | 'units' | 'operations'>('nomenclature')

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
  } catch (e:any) {
    const msg = e?.response?.data?.detail || e?.message || 'Ошибка выгрузки групп'
    Notify.create({ type: 'negative', message: String(msg) })
  } finally {
    loading.value.groups = false
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
    // загрузим сохранённый выбор
    const sel = await api.get('/v1/odata/groups/selection')
    const ids: string[] = Array.isArray(sel.data?.ids) ? sel.data.ids : []
    selectedIds.value = new Set(ids)
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

loadConfig()
  
</script>

<style scoped>
.groups-box {
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  max-height: 360px;
  overflow: auto;
}
</style>