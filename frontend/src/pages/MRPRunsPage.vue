<template>
  <q-page padding>
    <div class="row items-center q-gutter-sm q-mb-md">
      <div class="text-h5">MRP планирование — прогоны</div>
      <q-space />
      <q-select v-model="form.horizon_days" :options="horizonOptions" dense outlined label="Горизонт (дн.)" style="width: 160px" />
      <q-toggle v-model="form.use_weekly" label="Weekly" dense />
      <q-btn color="primary" icon="play_arrow" label="Рассчитать" :loading="calcLoading" @click="onCalc" />
      <q-btn flat color="primary" icon="refresh" label="Обновить" :loading="loading" @click="fetchRuns" />
      <q-btn flat color="secondary" icon="tune" label="Конфигурация" @click="openCfg" />
    </div>

    <q-table
      :rows="rows"
      :columns="columns"
      row-key="run_id"
      :loading="loading"
      :pagination="pagination"
      @request="onRequest"
    >
      <template v-slot:body-cell-status="props">
        <q-td :props="props">
          <q-chip :color="statusColor(props.row.status)" text-color="white" size="sm">
            {{ props.row.status }}
          </q-chip>
        </q-td>
      </template>

      <template v-slot:body-cell-actions="props">
        <q-td :props="props">
          <q-btn dense flat icon="visibility" @click="goRun(props.row.run_id)" />
        </q-td>
      </template>
    </q-table>

    <!-- Диалог конфигурации планирования -->
    <q-dialog v-model="cfgDialog" persistent maximized>
      <q-card style="max-width: 1200px; width: 100%">
        <q-card-section class="row items-center q-gutter-sm">
          <div class="text-h6">Конфигурация планирования</div>
          <q-space />
          <q-btn dense flat round icon="close" @click="cfgDialog = false" />
        </q-card-section>

        <q-separator />

        <q-card-section>
          <div class="text-subtitle2 q-mb-sm">Активная конфигурация</div>
          <q-banner dense class="bg-grey-2 q-pa-sm q-mb-sm">
            <div class="row items-center q-gutter-sm">
              <div>ID: {{ activeCfg?.config?.id ?? activeCfg?.id ?? '—' }}</div>
              <div>Версия: {{ activeCfg?.config?.version ?? activeCfg?.version ?? '—' }}</div>
              <div class="text-grey">created_by: {{ activeCfg?.config?.created_by ?? activeCfg?.created_by ?? '—' }}</div>
              <div class="text-grey">created_at: {{ activeCfg?.config?.created_at ?? activeCfg?.created_at ?? '—' }}</div>
            </div>
          </q-banner>

          <q-expansion-item icon="data_object" label="Показать JSON активной конфигурации" dense>
            <q-card>
              <q-card-section>
                <pre class="json-box">{{ pretty(activeCfg?.config ?? activeCfg ?? {}) }}</pre>
              </q-card-section>
            </q-card>
          </q-expansion-item>
        </q-card-section>

        <q-separator />

        <q-card-section>
          <div class="row items-center q-gutter-sm q-mb-sm">
            <div class="text-subtitle2">Версии конфигурации</div>
            <q-space />
            <q-btn dense flat color="primary" icon="refresh" label="Обновить список" @click="loadConfigs" />
          </div>
          <q-table
            :rows="cfgRows"
            :columns="cfgColumns"
            row-key="id"
            :loading="cfgLoading"
            :pagination="cfgPagination"
            @request="onCfgRequest"
          >
            <template v-slot:body-cell-is_active="props">
              <q-td :props="props">
                <q-chip dense :color="props.row.is_active ? 'positive' : 'grey'" text-color="white">
                  {{ props.row.is_active ? 'active' : 'inactive' }}
                </q-chip>
              </q-td>
            </template>
            <template v-slot:body-cell-actions="props">
              <q-td :props="props">
                <q-btn dense flat icon="bolt" color="primary" :disable="props.row.is_active" @click="activateCfg(props.row.id)">
                  <q-tooltip>Активировать</q-tooltip>
                </q-btn>
              </q-td>
            </template>
          </q-table>
        </q-card-section>

        <q-separator />

        <q-card-section>
          <div class="text-subtitle2 q-mb-sm">Создать новую версию</div>
          <div class="row q-col-gutter-md">
            <div class="col-12 col-md-8">
              <q-input v-model="cfgCreate.json" type="textarea" outlined autogrow :input-style="{ fontFamily: 'monospace' }"
                       label="JSON конфигурации (минимум — {} для копии активной)" />
            </div>
            <div class="col-12 col-md-4">
              <q-input v-model="cfgCreate.comment" outlined label="Комментарий" class="q-mb-sm" />
              <q-input v-model="cfgCreate.created_by" outlined label="Автор" class="q-mb-sm" />
              <q-toggle v-model="cfgCreate.activate" label="Сразу активировать" />
              <div class="q-mt-md">
                <q-btn color="primary" label="Создать" :loading="cfgCreateLoading" @click="createCfg" />
              </div>
            </div>
          </div>
        </q-card-section>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useQuasar } from 'quasar'
import type { QTableColumn } from 'quasar'
import {
  listPlanningRuns,
  startPlanningRun,
  type PlanningRunRow,
  listPlanningConfigs,
  getActivePlanningConfig,
  createPlanningConfig,
  activatePlanningConfig
} from '../services/api'

const $q = useQuasar()
const router = useRouter()

const rows = ref<PlanningRunRow[]>([])
const loading = ref(false)
const calcLoading = ref(false)
const pagination = ref({
  page: 1,
  rowsPerPage: 10,
  rowsNumber: 0
})

const horizonOptions = [30, 60, 90, 120]
const form = reactive({
  horizon_days: 90 as number,
  use_weekly: true as boolean
})

type Col = {
  name: string
  label: string
  field: string | ((row: any) => any)
  align?: 'left' | 'right' | 'center'
  sortable?: boolean
  format?: (val: any) => any
}

const columns = ref<Col[]>([
  { name: 'run_id', label: 'RUN', field: 'run_id', align: 'left', sortable: true },
  { name: 'status', label: 'Статус', field: 'status', align: 'left', sortable: true },
  { name: 'started_at', label: 'Старт', field: 'started_at', align: 'left', sortable: true },
  { name: 'finished_at', label: 'Финиш', field: 'finished_at', align: 'left', sortable: true },
  { name: 'horizon_days', label: 'Горизонт', field: 'horizon_days', align: 'right', sortable: true },
  { name: 'use_weekly', label: 'Weekly', field: (r: any) => (r.use_weekly ? 'Да' : 'Нет'), align: 'center' },
  { name: 'order_count', label: 'Заказы', field: 'order_count', align: 'right', sortable: true },
  { name: 'purchase_count', label: 'Закупки', field: 'purchase_count', align: 'right', sortable: true },
  { name: 'overload_buckets', label: 'Перегрузы', field: 'overload_buckets', align: 'right', sortable: true },
  { name: 'actions', label: 'Действия', field: 'run_id', align: 'right' }
])

function statusColor(s?: string) {
  const val = (s || '').toUpperCase()
  if (val === 'SUCCESS') return 'positive'
  if (val === 'RUNNING') return 'primary'
  if (val === 'FAILED') return 'negative'
  return 'grey'
}

async function fetchRuns() {
  loading.value = true
  try {
    const limit = pagination.value.rowsPerPage
    const offset = (pagination.value.page - 1) * pagination.value.rowsPerPage
    const resp = await listPlanningRuns({ limit, offset })
    rows.value = resp.rows || []
    pagination.value.rowsNumber = resp.total || 0
  } catch (e) {
    console.error('Failed to load runs', e)
    $q.notify({ type: 'negative', message: 'Не удалось загрузить список прогонов' })
  } finally {
    loading.value = false
  }
}

function onRequest(ctx: any) {
  if (ctx && ctx.pagination) {
    pagination.value = ctx.pagination
  }
  fetchRuns()
}

async function onCalc() {
  calcLoading.value = true
  try {
    const res = await startPlanningRun({
      horizon_days: form.horizon_days,
      use_weekly: form.use_weekly,
      started_by: 'ui'
    })
    await fetchRuns()
    if (res && res.run_id) {
      $q.notify({ type: 'positive', message: `RUN #${res.run_id} создан` })
      goRun(res.run_id)
    }
  } catch (e: any) {
    console.error('Failed to start run', e)
    const detail = e?.response?.data?.detail ?? e?.response?.data ?? e?.message ?? e
    let text = ''
    if (typeof detail === 'string') {
      text = detail
    } else {
      try {
        text = JSON.stringify(detail)
      } catch {
        text = String(detail)
      }
    }
    $q.notify({ type: 'negative', message: `Ошибка запуска расчёта: ${text}` })
  } finally {
    calcLoading.value = false
  }
}

function goRun(runId: number) {
  router.push({ name: 'mrp-result', params: { runId: String(runId) } })
}

/* ---- Конфигурация планирования (диалог) ---- */
const cfgDialog = ref(false)
const cfgLoading = ref(false)
const cfgPagination = ref({ page: 1, rowsPerPage: 10, rowsNumber: 0 })
const cfgRows = ref<any[]>([])
const activeCfg = ref<any | null>(null)
const cfgCreateLoading = ref(false)
const cfgCreate = reactive({
  json: '',
  comment: '',
  created_by: 'ui',
  activate: false
})

const cfgColumns: QTableColumn<any>[] = [
  { name: 'id', label: 'ID', field: 'id', align: 'right', sortable: true },
  { name: 'version', label: 'Версия', field: 'version', align: 'right', sortable: true },
  { name: 'is_active', label: 'Статус', field: 'is_active', align: 'left', sortable: true },
  { name: 'created_at', label: 'Создано', field: 'created_at', align: 'left', sortable: true },
  { name: 'created_by', label: 'Автор', field: 'created_by', align: 'left', sortable: true },
  { name: 'comment', label: 'Комментарий', field: 'comment', align: 'left' },
  { name: 'actions', label: 'Действия', field: 'id', align: 'right' }
]

function pretty(obj: any) {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

async function loadActiveConfig() {
  try {
    const resp = await getActivePlanningConfig()
    // ответ содержит {status:'ok', config:{...}}
    activeCfg.value = resp?.config || resp
  } catch (e) {
    console.error('Failed to load active config', e)
  }
}

async function loadConfigs() {
  cfgLoading.value = true
  try {
    const limit = cfgPagination.value.rowsPerPage
    const offset = (cfgPagination.value.page - 1) * cfgPagination.value.rowsPerPage
    const resp = await listPlanningConfigs({ limit, offset })
    cfgRows.value = resp?.rows || []
    cfgPagination.value.rowsNumber = resp?.total || 0
  } catch (e) {
    console.error('Failed to load configs', e)
    $q.notify({ type: 'negative', message: 'Не удалось загрузить версии конфигурации' })
  } finally {
    cfgLoading.value = false
  }
}

function onCfgRequest(ctx: any) {
  if (ctx?.pagination) cfgPagination.value = ctx.pagination
  loadConfigs()
}

async function activateCfg(id: number) {
  try {
    await activatePlanningConfig(Number(id))
    $q.notify({ type: 'positive', message: `Версия #${id} активирована` })
    await Promise.all([loadConfigs(), loadActiveConfig()])
  } catch (e) {
    console.error('Failed to activate config', e)
    $q.notify({ type: 'negative', message: 'Ошибка активации конфигурации' })
  }
}

async function createCfg() {
  cfgCreateLoading.value = true
  try {
    let body: any = {}
    if ((cfgCreate.json || '').trim().length > 0) {
      try {
        body.config = JSON.parse(cfgCreate.json)
      } catch {
        $q.notify({ type: 'warning', message: 'JSON конфигурации некорректен' })
        cfgCreateLoading.value = false
        return
      }
    } else {
      body.config = {} // пустой — сервер создаст версию с переданным объектом
    }
    body.comment = cfgCreate.comment || undefined
    body.created_by = cfgCreate.created_by || undefined
    body.activate = !!cfgCreate.activate
    const resp = await createPlanningConfig(body)
    $q.notify({ type: 'positive', message: `Создана версия #${resp?.created?.id ?? ''}` })
    cfgCreate.json = ''
    cfgCreate.comment = ''
    await Promise.all([loadConfigs(), loadActiveConfig()])
  } catch (e) {
    console.error('Failed to create config', e)
    $q.notify({ type: 'negative', message: 'Ошибка создания конфигурации' })
  } finally {
    cfgCreateLoading.value = false
  }
}

function openCfg() {
  cfgDialog.value = true
  loadActiveConfig()
  loadConfigs()
}

onMounted(() => {
  fetchRuns()
})
</script>

<style scoped>
.text-h5 {
  font-weight: 600;
}
</style>