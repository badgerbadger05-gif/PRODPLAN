<template>
  <q-page padding class="production-control-page">
    <div class="row items-center q-gutter-sm q-mb-md">
      <div>
        <div class="text-h5">Журнал заказов на производство</div>
        <div class="text-caption text-grey-7">Строки заказов по деталям, цехам, датам и выдаче комплектующих</div>
      </div>
      <q-space />
      <q-btn color="primary" icon="print" label="Печать маршрутных" :disable="selected.length === 0" @click="printSelected" />
      <q-btn color="secondary" icon="inventory_2" label="Создать выдачу" :disable="selected.length === 0" :loading="issueLoading" @click="createIssues" />
      <q-btn flat color="primary" icon="settings" label="Настройки складов" @click="openSettings" />
      <q-btn flat color="primary" icon="refresh" label="Обновить" :loading="loading" @click="fetchRows" />
    </div>

    <div class="row q-col-gutter-sm q-mb-sm">
      <div class="col-12 col-md-4">
        <q-input v-model="filters.search" dense outlined clearable debounce="350" label="Поиск: заказ, деталь, артикул" @update:model-value="fetchRows">
          <template #prepend><q-icon name="search" /></template>
        </q-input>
      </div>
      <div class="col-6 col-md-2">
        <q-select v-model="filters.status" dense outlined clearable emit-value map-options :options="statusOptions" label="Статус" @update:model-value="fetchRows" />
      </div>
      <div class="col-6 col-md-2">
        <q-select v-model="filters.workshop_id" dense outlined clearable emit-value map-options :options="workshopOptions" label="Цех" @update:model-value="fetchRows" />
      </div>
      <div class="col-6 col-md-2">
        <q-input v-model="filters.date_from" dense outlined type="date" label="Открыт с" @update:model-value="fetchRows" />
      </div>
      <div class="col-6 col-md-2">
        <q-input v-model="filters.date_to" dense outlined type="date" label="Открыт по" @update:model-value="fetchRows" />
      </div>
    </div>

    <q-table
      v-model:selected="selected"
      :rows="rows"
      :columns="columns"
      row-key="product_id"
      selection="multiple"
      :loading="loading"
      :pagination="pagination"
      @request="onRequest"
      binary-state-sort
      flat
      bordered
      class="control-table"
    >
      <template #body-cell-order_number="props">
        <q-td :props="props">
          <div class="text-weight-medium">№ {{ props.row.order_number }}</div>
          <div class="text-caption text-grey-7">{{ formatDate(props.row.order_date) }} · строка {{ props.row.line_number || '—' }}</div>
        </q-td>
      </template>

      <template #body-cell-item_name="props">
        <q-td :props="props">
          <div class="item-name">{{ props.row.item_name }}</div>
          <div class="text-caption text-grey-7">{{ props.row.item_article || props.row.item_code }}</div>
        </q-td>
      </template>

      <template #body-cell-qty="props">
        <q-td :props="props">
          <div>{{ formatQty(props.row.remaining_qty) }} / {{ formatQty(props.row.quantity) }} {{ displayUnit(props.row.unit) }}</div>
          <q-linear-progress :value="progressValue(props.row)" color="positive" track-color="grey-3" rounded size="6px" class="q-mt-xs" />
        </q-td>
      </template>

      <template #body-cell-workshop_name="props">
        <q-td :props="props">
          <div>{{ props.row.workshop_name || 'Не назначен' }}</div>
          <div class="text-caption text-grey-7">{{ props.row.stage_name || '' }}</div>
        </q-td>
      </template>

      <template #body-cell-dates="props">
        <q-td :props="props">
          <div>Старт: {{ formatDate(props.row.planned_start_date) }}</div>
          <div>Финиш: {{ formatDate(props.row.planned_finish_date) }}</div>
        </q-td>
      </template>

      <template #body-cell-status="props">
        <q-td :props="props">
          <q-select
            :model-value="props.row.status"
            dense
            borderless
            emit-value
            map-options
            :options="statusOptions"
            @update:model-value="(value) => changeStatus(props.row, value)"
          >
            <template #selected>
              <q-chip dense :color="statusColor(props.row.status)" text-color="white">{{ statusLabel(props.row.status) }}</q-chip>
            </template>
          </q-select>
        </q-td>
      </template>

      <template #body-cell-issue_status="props">
        <q-td :props="props">
          <q-chip dense :color="issueColor(props.row.issue_status)" text-color="white">{{ issueLabel(props.row.issue_status) }}</q-chip>
          <div v-if="props.row.issue_count" class="text-caption text-grey-7">документов: {{ props.row.issue_count }}</div>
        </q-td>
      </template>

      <template #body-cell-actions="props">
        <q-td :props="props" class="text-right">
          <q-btn dense flat round icon="inventory" @click="openMaterials(props.row)">
            <q-tooltip>Материалы по детали</q-tooltip>
          </q-btn>
          <q-btn dense flat round icon="print" @click="printOne(props.row)">
            <q-tooltip>Печатать маршрутный лист</q-tooltip>
          </q-btn>
        </q-td>
      </template>
    </q-table>

    <!-- Settings dialog: workshop -> warehouse bindings + ignored warehouses -->
    <q-dialog v-model="settingsDialog" persistent>
      <q-card style="min-width: 720px; max-width: 95vw;">
        <q-card-section class="row items-center q-pb-none">
          <div class="text-h6">Настройки складов</div>
          <q-space />
          <q-btn flat round dense icon="close" v-close-popup />
        </q-card-section>

        <q-card-section>
          <div class="text-subtitle1 q-mb-sm">Привязка участок → склад получатель</div>
          <q-table
            :rows="settings.workshop_warehouse_bindings"
            :columns="bindingsColumns"
            row-key="workshop_id"
            hide-bottom
            flat
            dense
            :loading="settingsLoading"
            :no-data-label="'Привязок ещё нет'"
          >
            <template #body-cell-actions="props">
              <q-td :props="props" class="text-right">
                <q-btn
                  flat
                  dense
                  round
                  color="negative"
                  icon="delete"
                  size="sm"
                  @click="removeBinding(props.row.workshop_id)"
                />
              </q-td>
            </template>
          </q-table>

          <div class="row q-col-gutter-sm q-mt-sm items-end">
            <div class="col-12 col-md-5">
              <q-select
                v-model="newBinding.workshop_id"
                dense
                outlined
                emit-value
                map-options
                :options="workshopOptions"
                label="Участок"
              />
            </div>
            <div class="col-12 col-md-5">
              <q-input
                v-model="newBinding.warehouse_ref1c"
                dense
                outlined
                label="Склад (Ref1C GUID)"
                placeholder="00000000-0000-0000-0000-000000000000"
              />
            </div>
            <div class="col-12 col-md-2">
              <q-btn
                color="primary"
                label="Добавить"
                icon="add"
                :disable="!newBinding.workshop_id || !newBinding.warehouse_ref1c"
                :loading="bindingSaving"
                @click="saveBinding"
              />
            </div>
          </div>
        </q-card-section>

        <q-separator />

        <q-card-section>
          <div class="text-subtitle1 q-mb-sm">Игнорируемые склады</div>
          <div class="text-caption text-grey-7 q-mb-sm">
            Эти склады не учитываются в расчёте обеспечения — например, изолятор брака.
          </div>
          <q-table
            :rows="settings.ignored_warehouses"
            :columns="ignoredColumns"
            row-key="warehouse_ref1c"
            hide-bottom
            flat
            dense
            :loading="settingsLoading"
            :no-data-label="'Игнор-список пуст'"
          >
            <template #body-cell-actions="props">
              <q-td :props="props" class="text-right">
                <q-btn
                  flat
                  dense
                  round
                  color="negative"
                  icon="delete"
                  size="sm"
                  @click="removeIgnored(props.row.warehouse_ref1c)"
                />
              </q-td>
            </template>
          </q-table>

          <div class="row q-col-gutter-sm q-mt-sm items-end">
            <div class="col-12 col-md-4">
              <q-input
                v-model="newIgnored.warehouse_ref1c"
                dense
                outlined
                label="Склад (Ref1C GUID)"
                placeholder="00000000-0000-0000-0000-000000000000"
              />
            </div>
            <div class="col-12 col-md-3">
              <q-input v-model="newIgnored.warehouse_name" dense outlined label="Название" />
            </div>
            <div class="col-12 col-md-3">
              <q-input v-model="newIgnored.reason" dense outlined label="Причина" />
            </div>
            <div class="col-12 col-md-2">
              <q-btn
                color="primary"
                label="Добавить"
                icon="add"
                :disable="!newIgnored.warehouse_ref1c"
                :loading="ignoredSaving"
                @click="saveIgnored"
              />
            </div>
          </div>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat label="Закрыть" v-close-popup />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="materialsDialog" maximized>
      <q-card>
        <q-card-section class="row items-center">
          <div>
            <div class="text-h6">Комплектующие под деталь</div>
            <div class="text-caption text-grey-7">{{ materials?.order_number }} · {{ materials?.item_name }} · {{ materials?.item_article }}</div>
          </div>
          <q-space />
          <q-btn dense flat round icon="close" @click="materialsDialog = false" />
        </q-card-section>
        <q-separator />
        <q-card-section>
          <q-table :rows="materials?.components || []" :columns="materialColumns" row-key="component_item_id" flat bordered :pagination="{ rowsPerPage: 50 }">
            <template #body-cell-required_qty="props">
              <q-td :props="props">{{ formatQty(props.row.required_qty) }} {{ displayUnit(props.row.unit) }}</q-td>
            </template>
          </q-table>
        </q-card-section>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useQuasar } from 'quasar'
import type { QTableColumn } from 'quasar'
import {
  createProductionMaterialIssues,
  deleteProductionControlIgnoredWarehouse,
  deleteProductionControlWorkshopBinding,
  getProductionControlMaterials,
  getProductionControlSettings,
  listResources,
  listProductionControlOrders,
  updateProductionControlOrderState,
  upsertProductionControlIgnoredWarehouse,
  upsertProductionControlWorkshopBinding,
  type IgnoredWarehouseEntry,
  type ProductionControlOrderRow,
  type ProductionControlSettings,
  type WorkshopWarehouseBinding
} from '../services/api'

const $q = useQuasar()

const rows = ref<ProductionControlOrderRow[]>([])
const selected = ref<ProductionControlOrderRow[]>([])
const loading = ref(false)
const issueLoading = ref(false)
const materialsDialog = ref(false)
const materials = ref<any | null>(null)
const workshopOptions = ref<Array<{ label: string; value: number }>>([])

const filters = reactive({
  search: '',
  status: null as string | null,
  workshop_id: null as number | null,
  date_from: '',
  date_to: ''
})

const pagination = ref({
  page: 1,
  rowsPerPage: 50,
  rowsNumber: 0
})

// Plan-aligned "Обеспечение" status set:
//   shortage / partial / ready / to_move / assembled / produced_partial / produced
// Plus an out-of-band 'cancelled' kept for manual admin overrides.
const statusOptions = [
  { label: 'Дефицит', value: 'shortage' },
  { label: 'Частично', value: 'partial' },
  { label: 'Обеспечен', value: 'ready' },
  { label: 'К перемещению', value: 'to_move' },
  { label: 'Собран', value: 'assembled' },
  { label: 'Произведен частично', value: 'produced_partial' },
  { label: 'Произведен', value: 'produced' },
  { label: 'Отменен', value: 'cancelled' }
]

const columns: QTableColumn<ProductionControlOrderRow>[] = [
  { name: 'order_number', label: 'Заказ', field: 'order_number', align: 'left', sortable: true },
  { name: 'item_name', label: 'Деталь', field: 'item_name', align: 'left', sortable: true },
  { name: 'qty', label: 'Остаток / заказ', field: 'remaining_qty', align: 'left', sortable: true },
  { name: 'workshop_name', label: 'Цех', field: 'workshop_name', align: 'left', sortable: true },
  { name: 'dates', label: 'Плановые даты', field: 'planned_start_date', align: 'left' },
  { name: 'status', label: 'Обеспечение', field: 'status', align: 'left' },
  { name: 'issue_status', label: 'Выдача', field: 'issue_status', align: 'left' },
  { name: 'actions', label: '', field: 'product_id', align: 'right' }
]

const materialColumns: QTableColumn<any>[] = [
  { name: 'item_name', label: 'Комплектующее', field: 'item_name', align: 'left', sortable: true },
  { name: 'item_article', label: 'Артикул', field: 'item_article', align: 'left', sortable: true },
  { name: 'qty_per_unit', label: 'На ед.', field: 'qty_per_unit', align: 'right', sortable: true },
  { name: 'required_qty', label: 'К выдаче', field: 'required_qty', align: 'right', sortable: true }
]

// ---------------------------------------------------------------------------
// Settings dialog (workshop->warehouse bindings + ignored warehouses).
// Backend: GET/PUT/DELETE /v1/production-control/settings/*.
// ---------------------------------------------------------------------------
const settingsDialog = ref(false)
const settingsLoading = ref(false)
const bindingSaving = ref(false)
const ignoredSaving = ref(false)
const settings = reactive<ProductionControlSettings>({
  workshop_warehouse_bindings: [],
  ignored_warehouses: []
})
const newBinding = reactive<{ workshop_id: number | null; warehouse_ref1c: string }>({
  workshop_id: null,
  warehouse_ref1c: ''
})
const newIgnored = reactive<{ warehouse_ref1c: string; warehouse_name: string; reason: string }>({
  warehouse_ref1c: '',
  warehouse_name: '',
  reason: ''
})

const bindingsColumns: QTableColumn<WorkshopWarehouseBinding>[] = [
  { name: 'workshop_name', label: 'Участок', field: r => r.workshop_name || `#${r.workshop_id}`, align: 'left' },
  { name: 'warehouse_ref1c', label: 'Склад (Ref1C)', field: 'warehouse_ref1c', align: 'left' },
  { name: 'actions', label: '', field: 'workshop_id', align: 'right' }
]

const ignoredColumns: QTableColumn<IgnoredWarehouseEntry>[] = [
  { name: 'warehouse_ref1c', label: 'Склад (Ref1C)', field: 'warehouse_ref1c', align: 'left' },
  { name: 'warehouse_name', label: 'Название', field: r => r.warehouse_name || '', align: 'left' },
  { name: 'reason', label: 'Причина', field: r => r.reason || '', align: 'left' },
  { name: 'actions', label: '', field: 'warehouse_ref1c', align: 'right' }
]

async function loadSettings() {
  settingsLoading.value = true
  try {
    const data = await getProductionControlSettings()
    settings.workshop_warehouse_bindings = data.workshop_warehouse_bindings || []
    settings.ignored_warehouses = data.ignored_warehouses || []
  } catch (e: any) {
    $q.notify({ type: 'negative', message: `Ошибка загрузки настроек: ${e?.message || e}` })
  } finally {
    settingsLoading.value = false
  }
}

async function openSettings() {
  settingsDialog.value = true
  await loadSettings()
}

async function saveBinding() {
  if (!newBinding.workshop_id || !newBinding.warehouse_ref1c.trim()) return
  bindingSaving.value = true
  try {
    await upsertProductionControlWorkshopBinding(
      newBinding.workshop_id,
      newBinding.warehouse_ref1c.trim()
    )
    newBinding.workshop_id = null
    newBinding.warehouse_ref1c = ''
    await loadSettings()
    $q.notify({ type: 'positive', message: 'Привязка сохранена' })
  } catch (e: any) {
    $q.notify({ type: 'negative', message: `Не удалось сохранить: ${e?.response?.data?.detail || e?.message || e}` })
  } finally {
    bindingSaving.value = false
  }
}

async function removeBinding(workshopId: number) {
  try {
    await deleteProductionControlWorkshopBinding(workshopId)
    await loadSettings()
  } catch (e: any) {
    $q.notify({ type: 'negative', message: `Не удалось удалить: ${e?.response?.data?.detail || e?.message || e}` })
  }
}

async function saveIgnored() {
  const ref = newIgnored.warehouse_ref1c.trim()
  if (!ref) return
  ignoredSaving.value = true
  try {
    await upsertProductionControlIgnoredWarehouse({
      warehouse_ref1c: ref,
      warehouse_name: newIgnored.warehouse_name.trim() || null,
      reason: newIgnored.reason.trim() || null
    })
    newIgnored.warehouse_ref1c = ''
    newIgnored.warehouse_name = ''
    newIgnored.reason = ''
    await loadSettings()
    $q.notify({ type: 'positive', message: 'Склад добавлен в игнор-список' })
  } catch (e: any) {
    $q.notify({ type: 'negative', message: `Не удалось сохранить: ${e?.response?.data?.detail || e?.message || e}` })
  } finally {
    ignoredSaving.value = false
  }
}

async function removeIgnored(warehouseRef1c: string) {
  try {
    await deleteProductionControlIgnoredWarehouse(warehouseRef1c)
    await loadSettings()
  } catch (e: any) {
    $q.notify({ type: 'negative', message: `Не удалось удалить: ${e?.response?.data?.detail || e?.message || e}` })
  }
}

function statusLabel(value: string) {
  return statusOptions.find(x => x.value === value)?.label || value
}

function statusColor(value: string) {
  return ({
    shortage: 'negative',
    partial: 'orange',
    ready: 'blue',
    to_move: 'indigo',
    assembled: 'purple',
    produced_partial: 'amber',
    produced: 'positive',
    cancelled: 'grey-7'
  } as Record<string, string>)[value] || 'grey'
}

function issueLabel(value: string) {
  return ({
    not_requested: 'Не запрошена',
    requested: 'Запрошена',
    issued: 'Выдано',
    exported: 'В 1С',
    error: 'Ошибка'
  } as Record<string, string>)[value] || value
}

function issueColor(value: string) {
  return ({
    not_requested: 'grey-7',
    requested: 'orange',
    issued: 'positive',
    exported: 'blue',
    error: 'negative'
  } as Record<string, string>)[value] || 'grey'
}

function formatQty(value: number) {
  return Number(value || 0).toLocaleString('ru-RU', { maximumFractionDigits: 3 })
}

function looksLikeGuid(value?: string | null) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || '').trim())
}

function displayUnit(value?: string | null) {
  const unit = String(value || '').trim()
  return looksLikeGuid(unit) ? '' : unit
}

function formatDate(value?: string | null) {
  if (!value) return '—'
  return String(value).slice(0, 10).split('-').reverse().join('.')
}

function progressValue(row: ProductionControlOrderRow) {
  const total = Number(row.quantity || 0)
  if (total <= 0) return 0
  return Math.max(0, Math.min(1, Number(row.produced_qty || 0) / total))
}

async function fetchRows() {
  loading.value = true
  try {
    const limit = pagination.value.rowsPerPage
    const offset = (pagination.value.page - 1) * limit
    const data = await listProductionControlOrders({
      search: filters.search || null,
      status: filters.status,
      workshop_id: filters.workshop_id,
      date_from: filters.date_from || null,
      date_to: filters.date_to || null,
      limit,
      offset
    })
    rows.value = data.rows
    pagination.value.rowsNumber = data.total
  } catch (e: any) {
    $q.notify({ type: 'negative', message: e?.response?.data?.detail || e?.message || 'Ошибка загрузки журнала' })
  } finally {
    loading.value = false
  }
}

function onRequest(props: any) {
  pagination.value = props.pagination
  fetchRows()
}

async function changeStatus(row: ProductionControlOrderRow, status: string) {
  const previous = row.status
  row.status = status
  try {
    await updateProductionControlOrderState(row.product_id, { status })
    $q.notify({ type: 'positive', message: 'Статус обновлен' })
  } catch (e: any) {
    row.status = previous
    $q.notify({ type: 'negative', message: e?.response?.data?.detail || e?.message || 'Не удалось обновить статус' })
  }
}

function routeSheetUrl(ids: number[]) {
  return `/api/v1/production-control/route-sheets/print?product_ids=${ids.join(',')}`
}

function printSelected() {
  const ids = selected.value.map(x => x.product_id)
  if (ids.length) window.open(routeSheetUrl(ids), '_blank')
}

function printOne(row: ProductionControlOrderRow) {
  window.open(routeSheetUrl([row.product_id]), '_blank')
}

async function createIssues() {
  issueLoading.value = true
  try {
    const result = await createProductionMaterialIssues({
      product_ids: selected.value.map(x => x.product_id)
    })
    const created = result?.created?.length || 0
    const errors = result?.errors?.length || 0
    $q.notify({ type: created ? 'positive' : 'warning', message: `Создано документов: ${created}${errors ? `, ошибок: ${errors}` : ''}` })
    selected.value = []
    await fetchRows()
  } catch (e: any) {
    $q.notify({ type: 'negative', message: e?.response?.data?.detail || e?.message || 'Не удалось создать выдачу' })
  } finally {
    issueLoading.value = false
  }
}

async function openMaterials(row: ProductionControlOrderRow) {
  try {
    materials.value = await getProductionControlMaterials(row.product_id)
    materialsDialog.value = true
  } catch (e: any) {
    $q.notify({ type: 'negative', message: e?.response?.data?.detail || e?.message || 'Не удалось загрузить материалы' })
  }
}

async function loadWorkshops() {
  try {
    const data = await listResources()
    workshopOptions.value = (data.rows || []).map((row: any) => ({
      label: String(row.resource_name || row.name || `Цех ${row.resource_id}`),
      value: Number(row.resource_id)
    }))
  } catch (e) {
    workshopOptions.value = []
  }
}

onMounted(async () => {
  await loadWorkshops()
  await fetchRows()
})
</script>

<style scoped>
.production-control-page {
  background: #fafafa;
}

.control-table {
  background: #fff;
}

.item-name {
  max-width: 420px;
  white-space: normal;
  line-height: 1.25;
}
</style>
