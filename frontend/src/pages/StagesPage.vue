<template>
 <q-page class="q-pa-lg">
    <div class="row justify-center">
      <div class="col-12">
        <q-card>
          <q-card-section class="row items-center justify-between">
            <div class="text-h5">Распределение этапов</div>
            <div class="row items-center q-gutter-sm">
              <div v-if="asOf" class="text-caption text-grey-7">
                Остаток на {{ formatAsOf(asOf) }}
              </div>
              <q-toggle
                v-model="aggregateByProduct"
                color="primary"
                dense
                label="Суммировать одинаковые по изделию"
              />
              <q-btn
                color="primary"
                icon="calculate"
                label="Распределить этапы"
                @click="calculate"
                :loading="loading"
              />
            </div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <div v-if="!loading && resources.length === 0" class="text-grey-7">
              Нет данных. Нажмите "Распределить этапы".
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
                  v-for="resource in resources"
                  :key="`tab-${resource.resource_id}`"
                  :name="resource.resource_id"
                  :label="`${resource.resource_name} (${formatQty(resource.norm_hours, 2)} н/ч)`"
                />
              </q-tabs>

              <q-separator class="q-mt-sm q-mb-md" />

              <q-tab-panels v-model="activeTab" animated>
                <q-tab-panel
                  v-for="resource in resources"
                  :key="`panel-${resource.resource_id}`"
                  :name="resource.resource_id"
                >
                  <div
                    v-for="prod in resource.products"
                    :key="prod.root_item_id + ':' + prod.root_item_code"
                    class="q-mb-xl"
                  >
                    <div class="text-subtitle1 q-py-xs stage-product-title">
                      {{ prod.root_item_name }} [{{ prod.root_item_code }}]
                    </div>

                    <q-table
                      :rows="aggregateByProduct ? aggregateComponents(prod.components) : prod.components"
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
                     <template #body-cell-norm_hours="props">
                       <q-td :props="props" class="text-right">
                         {{ formatQty(props.row.norm_hours, 2) }}
                       </q-td>
                     </template>
                     <template #body-cell-norm_hours_total="props">
                       <q-td :props="props" class="text-right">
                         {{ formatQty((props.row.norm_hours_total ?? (props.row.norm_hours * props.row.qty_per_unit)), 2) }}
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

interface DistributedComponent {
  item_id: number
  item_article?: string | null
  item_code: string
  item_name: string
  qty_per_unit: number
  stock_qty: number
  norm_hours: number
  norm_hours_total: number
  stage_id?: number | null
  stage_name?: string | null
}

interface ProductDistributionBlock {
  root_item_id: number
  root_item_code: string
  root_item_name: string
  components: DistributedComponent[]
}

interface ResourceDistributionResult {
  resource_id: number
  resource_name: string
  norm_hours: number
  products: ProductDistributionBlock[]
}

const asOf = ref<string | null>(null)
const resources = ref<ResourceDistributionResult[]>([])
const loading = ref(false)
// Убрана принудительная перерисовка через renderKey - источник проблем
// Группировка одинаковых деталей по изделию
const aggregateByProduct = ref(true)

const pagination = reactive({
  page: 1,
  rowsPerPage: 50,
  sortBy: 'item_name',
  descending: false
})

const TAB_KEY = 'resource_distribution_active_tab'
const activeTab = ref<number | null>(null)

watch(resources, (list) => {
  if (!Array.isArray(list) || list.length === 0) {
    activeTab.value = null
    return
  }
  const ids = list.map(r => Number(r.resource_id))
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
  { name: 'item_article', label: 'Артикул', field: 'item_article', align: 'left' as const, sortable: true },
  { name: 'item_name', label: 'Наименование', field: 'item_name', align: 'left' as const, sortable: true },
  { name: 'qty_per_unit', label: 'Кол-во на 1 изделие', field: 'qty_per_unit', align: 'right' as const, sortable: true },
  { name: 'stage_name', label: 'Этап', field: 'stage_name', align: 'left' as const, sortable: true },
  { name: 'stock_qty', label: 'Остаток', field: 'stock_qty', align: 'right' as const, sortable: true },
  { name: 'norm_hours', label: 'Норматив н/ч', field: 'norm_hours', align: 'right' as const, sortable: true },
  { name: 'norm_hours_total', label: 'Сумма н/ч', field: 'norm_hours_total', align: 'right' as const, sortable: true }
]

function formatQty(x: number | null | undefined, maxDigits = 3): string {
  const v = Number(x ?? 0)
  if (Number.isNaN(v)) return '0'
  return v.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: maxDigits })
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
    
    const { data } = await api.post('/v1/resources/calculate_distribution', {})
    asOf.value = data?.asOf || null
    
    resources.value = Array.isArray(data?.resources) ? data.resources : []

    Notify.create({ type: 'positive', message: 'Распределение по участкам рассчитано' })
  } catch (err: any) {
    const msg = err?.response?.data?.detail || 'Ошибка расчета распределения'
    Notify.create({ type: 'negative', message: msg })
  } finally {
    loading.value = false
  }
}

/**
 * Агрегация одинаковых деталей по изделию:
 * - Группируем по (item_id, stage_id) внутри одного продукта
 * - qty_per_unit = сумма qty
 * - norm_hours_total = сумма total по группе (с фолбеком на norm_hours * qty)
 * - norm_hours (за единицу) оставляем как у базовой строки
 * - stock_qty берем из первой строки (остаток — справочный параметр)
 */
function aggregateComponents(rows: DistributedComponent[]): DistributedComponent[] {
  try {
    const map = new Map<string, DistributedComponent>()
    for (const r of (rows || [])) {
      const key = `${r.item_id}:${r.stage_id ?? 'null'}`
      const qty = Number(r?.qty_per_unit ?? 0)
      const perUnit = Number(r?.norm_hours ?? 0)
      const total = r?.norm_hours_total != null
        ? Number(r.norm_hours_total)
        : perUnit * qty

      const ex = map.get(key)
      if (!ex) {
        map.set(key, {
          ...r,
          qty_per_unit: qty,
          norm_hours_total: total
        } as any)
      } else {
        ex.qty_per_unit = Number(ex.qty_per_unit ?? 0) + qty
        // norm_hours за единицу оставляем как было (предполагается одинаковым для одного item_id)
        ex.norm_hours_total = Number(ex.norm_hours_total ?? 0) + total
      }
    }
    return Array.from(map.values())
  } catch {
    return rows || []
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