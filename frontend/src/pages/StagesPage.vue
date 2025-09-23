<template>
 <q-page class="q-pa-lg">
    <div class="row justify-center">
      <div class="col-12">
        <q-card>
          <q-card-section class="row items-center justify-between">
            <div class="text-h5">Этапы производства</div>
            <div class="row items-center q-gutter-sm">
              <div v-if="asOf" class="text-caption text-grey-7">
                Остаток на {{ formatAsOf(asOf) }}
              </div>
              <q-btn
                color="primary"
                icon="calculate"
                label="Рассчитать этапы"
                @click="calculate"
                :loading="loading"
              />
            </div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <div v-if="!loading && stages.length === 0" class="text-grey-7">
              Нет данных. Нажмите "Рассчитать этапы".
            </div>

            <div v-else>
              <q-tabs
                v-model="activeTab"
                dense
                class="text-primary"
                active-color="primary"
                indicator-color="primary"
                align="left"
                narrow-indicator
              >
                <q-tab
                  v-for="st in stages"
                  :key="`tab-${st.stage_id}`"
                  :name="st.stage_id"
                  :label="st.stage_name"
                />
              </q-tabs>

              <q-separator class="q-mt-sm q-mb-md" />

              <q-tab-panels v-model="activeTab" animated>
                <q-tab-panel
                  v-for="st in stages"
                  :key="`panel-${st.stage_id}`"
                  :name="st.stage_id"
                >
                  <div
                    v-for="prod in st.products"
                    :key="prod.root_item_id + ':' + prod.root_item_code"
                    class="q-mb-xl"
                  >
                    <div class="text-subtitle1 q-py-xs stage-product-title">
                      {{ prod.root_item_name }} [{{ prod.root_item_code }}]
                    </div>

                    <q-table
                      :rows="prod.components"
                      :columns="columns"
                      row-key="item_id"
                      flat
                      dense
                      :pagination="pagination"
                      hide-bottom
                      @request="onTableRequest"
                    >
                      <template #body-cell-qty_per_unit="props">
                        <q-td :props="props" class="text-right">
                          {{ formatQty(props.row.qty_per_unit) }}
                        </q-td>
                      </template>
                      <template #body-cell-stock_qty="props">
                        <q-td :props="props" class="text-right">
                          {{ formatQty(props.row.stock_qty) }}
                        </q-td>
                      </template>
                      <template #body-cell-min_batch="props">
                        <q-td :props="props" class="text-right">
                          {{ props.row.min_batch != null ? formatQty(props.row.min_batch) : '—' }}
                        </q-td>
                      </template>
                      <template #body-cell-max_batch="props">
                        <q-td :props="props" class="text-right">
                          {{ props.row.max_batch != null ? formatQty(props.row.max_batch) : '—' }}
                        </q-td>
                      </template>
                      <template #body-cell-replenishment_method="props">
                        <q-td :props="props">
                          {{ props.row.replenishment_method || '—' }}
                        </q-td>
                      </template>
                    </q-table>
                  </div>
                </q-tab-panel>
              </q-tab-panels>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { ref, reactive, watch, nextTick } from 'vue'
import { Notify } from 'quasar'
import api from '../services/api'

interface StageComponent {
  item_id: number
  item_code: string
  item_name: string
  qty_per_unit: number
  stock_qty: number
  replenishment_method?: string | null
  min_batch?: number | null
  max_batch?: number | null
}

interface StageProductBlock {
  root_item_id: number
  root_item_code: string
  root_item_name: string
  components: StageComponent[]
}

interface StageResult {
  stage_id: number
  stage_name: string
  products: StageProductBlock[]
}

const asOf = ref<string | null>(null)
const stages = ref<StageResult[]>([])
const loading = ref(false)
// Убрана принудительная перерисовка через renderKey - источник проблем

const pagination = reactive({
  page: 1,
  rowsPerPage: 50,
  sortBy: 'item_name',
  descending: false
})

const TAB_KEY = 'stages_active_tab'
const activeTab = ref<number | null>(null)

watch(stages, (list) => {
  if (!Array.isArray(list) || list.length === 0) {
    activeTab.value = null
    return
  }
  const ids = list.map(s => Number(s.stage_id))
  const savedRaw = localStorage.getItem(TAB_KEY)
  const saved = savedRaw != null ? Number(savedRaw) : null
  if (saved != null && ids.includes(saved)) {
    activeTab.value = saved
  } else {
    activeTab.value = ids[0]!
  }
})

watch(activeTab, (val) => {
  if (val != null) {
    localStorage.setItem(TAB_KEY, String(val))
  }
})

const columns = [
  { name: 'item_code', label: 'Код', field: 'item_code', align: 'left' as const, sortable: true },
  { name: 'item_name', label: 'Наименование', field: 'item_name', align: 'left' as const, sortable: true },
  { name: 'qty_per_unit', label: 'Кол-во на 1 изделие', field: 'qty_per_unit', align: 'right' as const, sortable: true },
  { name: 'min_batch', label: 'Минимальная партия запуска', field: 'min_batch', align: 'right' as const, sortable: false },
  { name: 'max_batch', label: 'Максимальная партия запуска', field: 'max_batch', align: 'right' as const, sortable: false },
  { name: 'stock_qty', label: 'Остаток', field: 'stock_qty', align: 'right' as const, sortable: true },
  { name: 'replenishment_method', label: 'Метод пополнения', field: 'replenishment_method', align: 'left' as const, sortable: true }
]

function formatQty(x: number | null | undefined): string {
  const v = Number(x ?? 0)
  if (Number.isNaN(v)) return '0'
  // до 3 знаков, как DECIMAL(10,3) в БД
  return v.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 3 })
}

function formatAsOf(iso: string): string {
  try {
    const d = new Date(iso)
    if (String(d) === 'Invalid Date') return iso
    return d.toLocaleString('ru-RU')
  } catch {
    return iso
  }
}

// Обработчик для QTable пагинации
function onTableRequest(props: any) {
  const { page, rowsPerPage, sortBy, descending } = props.pagination
  pagination.page = page
  pagination.rowsPerPage = rowsPerPage
  pagination.sortBy = sortBy
  pagination.descending = descending
}

async function calculate() {
  if (loading.value) return // Предотвращаем множественные запуски
  
  try {
    loading.value = true
    
    const { data } = await api.post('/v1/stages/calculate', {})
    asOf.value = data?.asOf || null
    
    // Простая и надёжная установка данных без избыточной обработки
    stages.value = Array.isArray(data?.stages) ? data.stages.map((st: any) => ({
      stage_id: Number(st.stage_id),
      stage_name: String(st.stage_name || ''),
      products: Array.isArray(st.products) ? st.products.map((p: any) => ({
        root_item_id: Number(p.root_item_id),
        root_item_code: String(p.root_item_code || ''),
        root_item_name: String(p.root_item_name || ''),
        components: Array.isArray(p.components) ? p.components.map((c: any) => ({
          item_id: Number(c.item_id),
          item_code: String(c.item_code || ''),
          item_name: String(c.item_name || ''),
          qty_per_unit: Number(c.qty_per_unit || 0),
          stock_qty: Number(c.stock_qty || 0),
          replenishment_method: c.replenishment_method ?? null,
          min_batch: c.min_batch == null ? null : Number(c.min_batch),
          max_batch: c.max_batch == null ? null : Number(c.max_batch)
        })) : []
      })) : []
    })) : []

    Notify.create({ type: 'positive', message: 'Этапы рассчитаны' })
  } catch (err: any) {
    const msg = err?.response?.data?.detail || 'Ошибка расчёта этапов'
    Notify.create({ type: 'negative', message: msg })
  } finally {
    loading.value = false
  }
}
  
</script>

<style scoped>
.stage-product-title {
  background-color: #f6f6f6;
  border: 1px solid #e0e0e0;
  padding-left: 8px;
}
</style>