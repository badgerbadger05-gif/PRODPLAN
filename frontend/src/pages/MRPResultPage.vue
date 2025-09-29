<template>
  <q-page padding>
    <div class="row items-center q-gutter-sm q-mb-md">
      <div class="text-h5">Результаты прогона MRP #{{ runId }}</div>
      <q-space />
      <q-chip v-if="summary?.run?.status" :color="statusColor(summary.run.status)" text-color="white" size="sm">
        {{ summary.run.status }}
      </q-chip>
    </div>

    <div class="row q-col-gutter-md q-mb-md">
      <div class="col-12 col-md-3">
        <q-card flat bordered>
          <q-card-section>
            <div class="text-subtitle2">RUN</div>
            <div class="text-h6">{{ runId }}</div>
          </q-card-section>
          <q-separator />
          <q-card-section>
            <div class="row items-center">
              <div class="col text-caption text-grey">Старт</div>
              <div class="col-auto">{{ summary?.run?.started_at || '—' }}</div>
            </div>
            <div class="row items-center">
              <div class="col text-caption text-grey">Финиш</div>
              <div class="col-auto">{{ summary?.run?.finished_at || '—' }}</div>
            </div>
            <div class="row items-center">
              <div class="col text-caption text-grey">Горизонт</div>
              <div class="col-auto">{{ summary?.run?.horizon_days ?? '—' }}</div>
            </div>
            <div class="row items-center">
              <div class="col text-caption text-grey">Weekly</div>
              <div class="col-auto">{{ (summary?.run?.use_weekly ? 'Да' : 'Нет') }}</div>
            </div>
          </q-card-section>
        </q-card>
      </div>

      <div class="col-12 col-md-9">
        <q-card flat bordered>
          <q-card-section>
            <div class="row q-col-gutter-md">
              <div class="col-6 col-md-3">
                <div class="text-caption text-grey">Производственные заказы</div>
                <div class="text-h6">{{ summary?.counts?.production_orders ?? 0 }}</div>
              </div>
              <div class="col-6 col-md-3">
                <div class="text-caption text-grey">Заявки на закупку</div>
                <div class="text-h6">{{ summary?.counts?.purchase_requests ?? 0 }}</div>
              </div>
              <div class="col-6 col-md-3">
                <div class="text-caption text-grey">Перегруженные бакеты</div>
                <div class="text-h6">{{ summary?.capacity?.overloaded_buckets ?? 0 }}</div>
              </div>
              <div class="col-6 col-md-3">
                <div class="text-caption text-grey">Суммарный перегруз (ч)</div>
                <div class="text-h6">{{ fmt(summary?.capacity?.overload_total) }}</div>
              </div>
            </div>
          </q-card-section>
          <q-separator />
          <q-card-section v-if="(summary?.warnings || []).length > 0">
            <q-expansion-item
              icon="warning"
              label="Предупреждения"
              caption="Нажмите, чтобы развернуть"
              dense
              switch-toggle-side
            >
              <div class="row q-col-gutter-xs q-pt-sm">
                <q-chip v-for="(w, idx) in summary?.warnings" :key="idx" color="orange" text-color="black" outline size="sm">
                  {{ warnText(w) }}
                </q-chip>
              </div>
            </q-expansion-item>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- НОВЫЕ БЛОКИ С РЕЗУЛЬТАТАМИ -->
    <div class="row q-col-gutter-md q-mb-md">
      <!-- Рекомендуемые заказы на производство -->
      <div class="col-12 col-lg-6">
        <q-card flat bordered>
          <q-card-section>
            <div class="text-h6">Рекомендуемые заказы на производство</div>
          </q-card-section>
          <q-separator />
          <!-- Если есть группировка по участкам — показываем её -->
          <template v-if="groupedProdRows.length">
            <q-table
              :rows="groupedProdRows"
              :columns="recommendedProdColumns"
              row-key="area_id"
              :loading="prod.loading"
              :pagination="{ rowsPerPage: 20 }"
              hide-header
            >
              <template v-slot:body="props">
                <q-tr :props="props" :key="`g_${props.row.area_id}`">
                  <q-td colspan="100%" class="bg-grey-2">
                    <div class="text-subtitle1">
                      <strong>Производственный участок:</strong> {{ props.row.area_name }}
                    </div>
                  </q-td>
                </q-tr>
                <q-tr v-for="order in props.row.orders" :key="order.order_id" :props="props">
                  <q-td key="item_name" :props="props">
                    <div>{{ order.item_name }}</div>
                    <div class="text-caption text-grey">{{ order.item_article }}</div>
                  </q-td>
                  <q-td key="qty" :props="props">
                    {{ fmt(order.qty) }}
                  </q-td>
                  <q-td key="need_date" :props="props">
                    {{ order.need_date }}
                  </q-td>
                </q-tr>
              </template>
            </q-table>
          </template>
          <!-- Фолбэк: показываем простой список рекомендованных производственных заказов -->
          <template v-else>
            <q-table
              :rows="plainProdRows"
              :columns="recommendedProdColumns"
              row-key="order_id"
              :loading="prod.loading"
              :pagination="{ rowsPerPage: 20 }"
            />
          </template>
        </q-card>
      </div>

      <!-- Рекомендуемые заказы на закупку -->
      <div class="col-12 col-lg-6">
        <q-card flat bordered>
          <q-card-section>
            <div class="text-h6">Рекомендуемые заказы на закупку</div>
          </q-card-section>
          <q-separator />
          <q-table
            :rows="purch.rows"
            :columns="recommendedPurchColumns"
            row-key="purchase_id"
            :loading="purch.loading"
            :pagination="{ rowsPerPage: 10 }"
          />
        </q-card>
      </div>
    </div>

    <!-- Вкладки для детального анализа (можно оставить ниже) -->
    <q-separator class="q-my-lg" />
    <div class="text-h6 q-mb-md">Детальный анализ</div>
    <q-tabs v-model="tab" class="text-primary q-mb-sm" dense>
      <q-tab name="production" icon="build" label="Производство (детально)" />
      <q-tab name="purchases" icon="shopping_cart" label="Закупки (детально)" />
      <q-tab name="capacity" icon="bar_chart" label="Мощности" />
      <q-tab name="pegging" icon="device_hub" label="Pegging" />
      <q-tab name="components" icon="list" label="Компоненты заказа" />
    </q-tabs>
    <q-separator />

    <q-tab-panels v-model="tab" animated>
      <!-- Production -->
      <q-tab-panel name="production">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-select v-model="prod.filter.bucket_type" :options="bucketOptions" dense outlined label="Бакет" style="width: 150px" />
          <q-input v-model="prod.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
          <q-input v-model="prod.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
          <q-btn dense color="primary" icon="search" @click="loadProduction()" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadProduction()" />
        </div>
        <q-table
          :rows="prod.rows"
          :columns="prod.columns"
          row-key="order_id"
          :loading="prod.loading"
          :pagination="prod.pagination"
          @request="onProdRequest"
        >
          <template v-slot:body-cell-stages="props">
            <q-td :props="props">
              <div v-if="(props.row.stages || []).length === 0" class="text-grey">—</div>
              <q-badge
                v-for="(s, i) in props.row.stages"
                :key="i"
                color="primary"
                outline
                class="q-mr-xs q-mb-xs"
              >
                {{ s.stage_id }} · {{ s.bucket_date }} · {{ fmt(s.hours) }} ч
              </q-badge>
            </q-td>
          </template>
        </q-table>
      </q-tab-panel>

      <!-- Purchases -->
      <q-tab-panel name="purchases">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-select v-model="purch.filter.bucket_type" :options="bucketOptions" dense outlined label="Бакет" style="width: 150px" />
          <q-input v-model="purch.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
          <q-input v-model="purch.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
          <q-btn dense color="primary" icon="search" @click="loadPurchases()" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadPurchases()" />
        </div>
        <q-table
          :rows="purch.rows"
          :columns="purch.columns"
          row-key="purchase_id"
          :loading="purch.loading"
          :pagination="purch.pagination"
          @request="onPurchRequest"
        />
      </q-tab-panel>

      <!-- Capacity -->
      <q-tab-panel name="capacity">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-select v-model="cap.filter.bucket_type" :options="bucketOptions" dense outlined label="Бакет" style="width: 150px" />
          <q-input v-model="cap.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
          <q-input v-model="cap.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
          <q-btn dense color="primary" icon="search" @click="loadCapacity()" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadCapacity()" />
        </div>
        <q-table
          :rows="cap.rows"
          :columns="cap.columns"
          row-key="key"
          :loading="cap.loading"
          :pagination="cap.pagination"
          @request="onCapRequest"
        />
      </q-tab-panel>

      <!-- Pegging -->
      <q-tab-panel name="pegging">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-input v-model.number="peg.filter.child_item_id" type="number" dense outlined label="Child item_id" style="width: 160px" />
          <q-input v-model.number="peg.filter.parent_item_id" type="number" dense outlined label="Parent item_id" style="width: 160px" />
          <q-input v-model="peg.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
          <q-input v-model="peg.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
          <q-btn dense color="primary" icon="search" @click="loadPegging()" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadPegging()" />
        </div>
        <q-table
          :rows="peg.rows"
          :columns="peg.columns"
          row-key="id"
          :loading="peg.loading"
          :pagination="peg.pagination"
          @request="onPegRequest"
        />
      </q-tab-panel>

      <!-- Order Components -->
      <q-tab-panel name="components">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-select
            v-model="comp.selectedOrderId"
            :options="comp.orderOptions"
            dense outlined
            emit-value map-options
            label="Выберите производственный заказ"
            style="min-width: 360px"
          />
          <q-btn dense color="primary" icon="visibility" label="Показать состав (по заказу)" @click="loadComponentsFromOrder" />

          <q-separator vertical inset class="q-mx-sm" />

          <q-input v-model.number="comp.selectedItemId" type="number" dense outlined label="item_id" style="width: 150px" />
          <q-input v-model.number="comp.selectedQty" type="number" dense outlined label="qty" style="width: 120px" />
          <q-btn dense color="primary" icon="search" label="Показать состав" @click="fetchFullTree" />
        </div>

        <q-table
          :rows="comp.rows"
          :columns="comp.columns"
          row-key="id"
          :loading="comp.loading"
          :pagination="{ page: 1, rowsPerPage: 1000 }"
          :separator="'cell'"
          :grid="false"
        >
          <template v-slot:body="props">
            <q-tr :props="props">
              <q-td auto-width>
                <q-btn size="sm" color="primary" round flat dense :icon="props.expand ? 'expand_more' : 'chevron_right'" @click="props.expand = !props.expand" />
              </q-td>
              <q-td v-for="col in comp.columns" :key="col.name" :props="props">
                <span v-if="col.name === 'name'">{{ props.row.name }}</span>
                <span v-else-if="col.name === 'article'">{{ props.row.article || '—' }}</span>
                <span v-else-if="col.name === 'qty'">{{ fmt(props.row.computed?.treeQty ?? 0) }}</span>
                <span v-else-if="col.name === 'stage'">{{ props.row.stage ? (props.row.stage as any).name || (props.row.stage as any).id : '—' }}</span>
              </q-td>
            </q-tr>
            <q-tr v-show="props.expand" :props="props">
              <q-td :colspan="comp.columns.length + 1" class="q-pa-none">
                <q-table
                  flat
                  :rows="props.row.children || []"
                  :columns="comp.columns"
                  row-key="id"
                  hide-bottom
                  :separator="'cell'"
                >
                  <template v-slot:body="childProps">
                    <q-tr :props="childProps">
                      <q-td auto-width>
                        <q-space />
                      </q-td>
                      <q-td v-for="col in comp.columns" :key="col.name" :props="childProps">
                        <span v-if="col.name === 'name'">{{ childProps.row.name }}</span>
                        <span v-else-if="col.name === 'article'">{{ childProps.row.article || '—' }}</span>
                        <span v-else-if="col.name === 'qty'">{{ fmt(childProps.row.computed?.treeQty ?? 0) }}</span>
                        <span v-else-if="col.name === 'stage'">{{ childProps.row.stage ? (childProps.row.stage as any).name || (childProps.row.stage as any).id : '—' }}</span>
                      </q-td>
                    </q-tr>
                  </template>
                </q-table>
              </q-td>
            </q-tr>
          </template>
        </q-table>
      </q-tab-panel>
    </q-tab-panels>
  </q-page>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import api, {
  getPlanningRunSummary,
  getPlanningResultProduction,
  getPlanningResultPurchases,
  getPlanningResultCapacity,
  getPlanningResultPegging,
  getSpecificationFull,
  listItems,
  listResources
} from '../services/api'
import type { QTableColumn } from 'quasar'
import type { SpecNode } from '../services/api'
const prodColumns: QTableColumn<any>[] = [
  { name: 'order_id', label: 'Order', field: 'order_id', align: 'left', sortable: true },
  { name: 'item_id', label: 'Item', field: 'item_id', align: 'right', sortable: true },
  { name: 'qty', label: 'Qty', field: 'qty', align: 'right', sortable: true },
  { name: 'need_date', label: 'Need', field: 'need_date', align: 'left', sortable: true },
  { name: 'start_date', label: 'Start', field: 'start_date', align: 'left', sortable: true },
  { name: 'finish_date', label: 'Finish', field: 'finish_date', align: 'left', sortable: true },
  { name: 'bucket_type', label: 'Bucket', field: 'bucket_type', align: 'left', sortable: true },
  { name: 'bucket_date', label: 'Bucket date', field: 'bucket_date', align: 'left', sortable: true },
  { name: 'priority_index', label: 'Prio', field: 'priority_index', align: 'right', sortable: true },
  { name: 'stages', label: 'Stages', field: 'stages', align: 'left' }
]

const recommendedProdColumns: QTableColumn<any>[] = [
  { name: 'item_name', label: 'Номенклатура', field: 'item_name', align: 'left' },
  { name: 'qty', label: 'Количество', field: 'qty', align: 'right' },
  { name: 'need_date', label: 'Требуемая дата', field: 'need_date', align: 'left' }
];

const recommendedPurchColumns: QTableColumn<any>[] = [
  { name: 'item_name', label: 'Номенклатура', field: (r: any) => (itemMap.value?.[r.item_id]?.item_name) ?? `Номенклатура #${r.item_id}`, align: 'left', sortable: true },
  { name: 'item_article', label: 'Артикул', field: (r: any) => (itemMap.value?.[r.item_id]?.item_article) ?? '', align: 'left', sortable: true },
  { name: 'qty', label: 'Количество', field: 'qty', align: 'right', sortable: true, format: (val) => fmt(val) },
  { name: 'need_date', label: 'Требуемая дата', field: 'need_date', align: 'left', sortable: true },
  { name: 'order_date', label: 'Дата заказа', field: 'order_date', align: 'left', sortable: true }
];

const purchColumns: QTableColumn<any>[] = [
  { name: 'purchase_id', label: 'Purchase', field: 'purchase_id', align: 'left', sortable: true },
  { name: 'item_id', label: 'Item', field: 'item_id', align: 'right', sortable: true },
  { name: 'qty', label: 'Qty', field: 'qty', align: 'right', sortable: true },
  { name: 'need_date', label: 'Need', field: 'need_date', align: 'left', sortable: true },
  { name: 'order_date', label: 'Order date', field: 'order_date', align: 'left', sortable: true },
  { name: 'lead_time_days', label: 'LT (d)', field: 'lead_time_days', align: 'right', sortable: true },
  { name: 'bucket_type', label: 'Bucket', field: 'bucket_type', align: 'left', sortable: true },
  { name: 'bucket_date', label: 'Bucket date', field: 'bucket_date', align: 'left', sortable: true },
  { name: 'priority_index', label: 'Prio', field: 'priority_index', align: 'right', sortable: true }
]

const capColumns: QTableColumn<any>[] = [
  { name: 'area_id', label: 'Area', field: 'area_id', align: 'right', sortable: true },
  { name: 'bucket_type', label: 'Bucket', field: 'bucket_type', align: 'left', sortable: true },
  { name: 'bucket_date', label: 'Date', field: 'bucket_date', align: 'left', sortable: true },
  { name: 'hours_planned', label: 'Planned (h)', field: 'hours_planned', align: 'right', sortable: true },
  { name: 'hours_available', label: 'Avail. (h)', field: 'hours_available', align: 'right', sortable: true },
  { name: 'overload_hours', label: 'Overload (h)', field: 'overload_hours', align: 'right', sortable: true }
]

const pegColumns: QTableColumn<any>[] = [
  { name: 'id', label: 'ID', field: 'id', align: 'right', sortable: true },
  { name: 'child_item_id', label: 'Child', field: 'child_item_id', align: 'right', sortable: true },
  { name: 'parent_item_id', label: 'Parent', field: 'parent_item_id', align: 'right', sortable: true },
  { name: 'qty_contribution', label: 'Qty contrib', field: 'qty_contribution', align: 'right', sortable: true },
  { name: 'need_date', label: 'Need date', field: 'need_date', align: 'left', sortable: true },
  { name: 'parent_need_date', label: 'Parent need', field: 'parent_need_date', align: 'left', sortable: true }
]

const route = useRoute()
const runId = Number(route.params.runId)

const summary = ref<any | null>(null)
const tab = ref<'production' | 'purchases' | 'capacity' | 'pegging' | 'components'>('production')

 // --- Справочники ---
 const itemMap = ref<{ [key: number]: any }>({})
 const areaMap = ref<{ [key: number]: string }>({})
 
 // --- Группировка для новых таблиц ---
 const groupedProductionOrders = ref<any[]>([])
// Итоговый источник строк для карточки «Рекомендуемые заказы на производство»
const groupedProdRows = computed(() => {
  const groups = groupedProductionOrders.value || []
  if (groups.length > 0) return groups
  // Фолбэк: если группировка по участкам пустая (нет stages/area_id),
  // показываем плоский список как одну группу
  const orders = (prod.rows || []).map((r: any) => ({
    ...r,
    item_name: (itemMap.value?.[r.item_id]?.item_name) ?? `Номенклатура #${r.item_id}`,
    item_article: (itemMap.value?.[r.item_id]?.item_article) ?? ''
  }))
  return orders.length ? [{ area_id: 0, area_name: '—', orders }] : []
})
// Плоский список для фолбэка
const plainProdRows = computed(() => {
  return (prod.rows || []).map((r: any) => ({
    ...r,
    item_name: (itemMap.value?.[r.item_id]?.item_name) ?? `Номенклатура #${r.item_id}`,
    item_article: (itemMap.value?.[r.item_id]?.item_article) ?? ''
  }))
})

function rebuildGroupedProductionOrders() {
  if (!prod.rows.length) {
    groupedProductionOrders.value = []
    return
  }

  const groups: { [key: string]: { area_id: number; area_name: string; orders: any[] } } = {}

  for (const order of prod.rows) {
    const areaId = order.stages?.[0]?.area_id ?? 0
    const areaName = areaId ? (areaMap.value[areaId] ?? `Участок #${areaId}`) : '—'
    const item = itemMap.value[order.item_id]

    if (!groups[areaId]) {
      groups[areaId] = {
        area_id: areaId,
        area_name: areaName,
        orders: []
      }
    }
    groups[areaId].orders.push({
      ...order,
      item_name: item?.item_name ?? `Номенклатура #${order.item_id}`,
      item_article: item?.item_article ?? ''
    })
  }
  groupedProductionOrders.value = Object.values(groups)
  try {
    console.log('MRP groupedProductionOrders', {
      prodRows: (prod.rows || []).length,
      groups: groupedProductionOrders.value.length,
      sampleGroup: groupedProductionOrders.value[0],
      sampleOrder: (prod.rows || [])[0]
    })
  } catch (e) {
    // no-op
  }
}

 // --- Справочники (moved above) ---

async function loadDictionaries() {
  try {
    const [itemsRes, resourcesRes] = await Promise.all([
      listItems({ limit: 10000, offset: 0 }),
      listResources({ limit: 1000, offset: 0 })
    ])

    itemMap.value = (itemsRes.rows || []).reduce((acc: { [key: number]: any }, item: any) => {
      acc[item.item_id] = item
      return acc
    }, {})

    areaMap.value = (resourcesRes.rows || []).reduce((acc: { [key: number]: string }, res: any) => {
      acc[res.resource_id] = res.resource_name
      return acc
    }, {})
    // словари загружены — пересобираем группировки для отображения названий/артикулов
    rebuildGroupedProductionOrders()
  } catch (e) {
    console.error('Failed to load dictionaries', e)
  }
}

// Догрузка недостающих словарей по фактическим строкам production/purchases
async function fillMissingDictionariesFromRows() {
  try {
    const missingItemIds = new Set<number>()
    const missingAreaIds = new Set<number>()

    // Из production
    for (const r of (prod.rows || [])) {
      if (r?.item_id && !itemMap.value[r.item_id]) missingItemIds.add(Number(r.item_id))
      const stages = Array.isArray(r?.stages) ? r.stages : []
      for (const s of stages) {
        const aid = s?.area_id
        if (aid && !areaMap.value[aid]) missingAreaIds.add(Number(aid))
      }
    }
    // Из purchases
    for (const r of (purch.rows || [])) {
      if (r?.item_id && !itemMap.value[r.item_id]) missingItemIds.add(Number(r.item_id))
    }

    // Ограничим объем единичных запросов
    const idsItems = Array.from(missingItemIds).slice(0, 500)
    const idsAreas = Array.from(missingAreaIds).slice(0, 200)

    const itemPromises = idsItems.map(id =>
      api.get(`/v1/items/${id}`).then(resp => ({ ok: true, data: resp.data })).catch(() => ({ ok: false }))
    )
    const areaPromises = idsAreas.map(id =>
      api.get(`/v1/resources/${id}`).then(resp => ({ ok: true, data: resp.data })).catch(() => ({ ok: false }))
    )

    const [itemResults, areaResults] = await Promise.all([
      Promise.all(itemPromises),
      Promise.all(areaPromises)
    ])

    for (const r of itemResults) {
      if ((r as any)?.ok && (r as any)?.data) {
        const it = (r as any).data
        if (it?.item_id != null) {
          itemMap.value[Number(it.item_id)] = it
        }
      }
    }
    for (const r of areaResults) {
      if ((r as any)?.ok && (r as any)?.data) {
        const a = (r as any).data
        if (a?.resource_id != null) {
          areaMap.value[Number(a.resource_id)] = String(a.resource_name ?? '')
        }
      }
    }
  } catch (e) {
    console.error('Failed to fill dictionaries from rows', e)
  }
}

const bucketOptions = [
  { label: 'Любой', value: undefined },
  { label: 'daily', value: 'daily' },
  { label: 'weekly', value: 'weekly' }
]

// Production state
const prod = reactive({
  rows: [] as any[],
  loading: false,
  filter: { bucket_type: undefined as 'daily' | 'weekly' | undefined, date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 20, rowsNumber: 0 },
  columns: prodColumns
})

// Purchases state
const purch = reactive({
  rows: [] as any[],
  loading: false,
  filter: { bucket_type: undefined as 'daily' | 'weekly' | undefined, date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 20, rowsNumber: 0 },
  columns: purchColumns
})

// Capacity state
const cap = reactive({
  rows: [] as any[],
  loading: false,
  filter: { bucket_type: undefined as 'daily' | 'weekly' | undefined, date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 30, rowsNumber: 0 },
  columns: capColumns
})

// Pegging state
const peg = reactive({
  rows: [] as any[],
  loading: false,
  filter: { child_item_id: undefined as number | undefined, parent_item_id: undefined as number | undefined, date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 30, rowsNumber: 0 },
  columns: pegColumns
})

// Order Components state
const comp = reactive({
  selectedOrderId: null as number | null,
  selectedItemId: null as number | null,
  selectedQty: null as number | null,
  rows: [] as SpecNode[],
  loading: false,
  orderOptions: [] as { label: string; value: number }[],
  columns: [
    { name: 'name', label: 'Компонент', field: 'name', align: 'left', sortable: true },
    { name: 'article', label: 'Артикул', field: 'article', align: 'left', sortable: true },
    { name: 'qty', label: 'Требуемое кол-во', field: (r: any) => r?.computed?.treeQty ?? 0, align: 'right', sortable: true },
    { name: 'stage', label: 'Этап', field: (r: any) => (r?.stage ? (r.stage as any).name || (r.stage as any).id : null), align: 'left' }
  ] as QTableColumn<any>[]
})

function fmt(v: any) {
  if (v === null || v === undefined) return '0'
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toFixed(2)
}

function statusColor(s?: string) {
  const val = (s || '').toUpperCase()
  if (val === 'SUCCESS') return 'positive'
  if (val === 'RUNNING') return 'primary'
  if (val === 'FAILED') return 'negative'
  return 'grey'
}

function warnText(w: any) {
  try {
    const code = w?.code ? String(w.code) : ''
    const msg = w?.msg ? String(w.msg) : ''
    return code ? `${code}: ${msg}` : msg
  } catch {
    return String(w)
  }
}

async function loadSummary() {
  try {
    summary.value = await getPlanningRunSummary(runId)
  } catch (e) {
    console.error('Failed to load summary', e)
  }
}

async function loadProduction() {
  prod.loading = true
  try {
    const limit = prod.pagination.rowsPerPage
    const offset = (prod.pagination.page - 1) * prod.pagination.rowsPerPage
    const resp = await getPlanningResultProduction(runId, {
      bucket_type: prod.filter.bucket_type,
      date_from: emptyToUndef(prod.filter.date_from),
      date_to: emptyToUndef(prod.filter.date_to),
      limit, offset
    })
    prod.rows = resp.rows || []
    prod.pagination.rowsNumber = resp.total || 0
    rebuildOrderOptions()
    // Явно пересобираем группы для «Рекомендуемые заказы на производство»
    rebuildGroupedProductionOrders()
  } catch (e) {
    console.error('Failed to load production', e)
  } finally {
    prod.loading = false
  }
}

async function loadPurchases() {
  purch.loading = true
  try {
    const limit = purch.pagination.rowsPerPage
    const offset = (purch.pagination.page - 1) * purch.pagination.rowsPerPage
    const resp = await getPlanningResultPurchases(runId, {
      bucket_type: purch.filter.bucket_type,
      date_from: emptyToUndef(purch.filter.date_from),
      date_to: emptyToUndef(purch.filter.date_to),
      limit, offset
    })
    purch.rows = resp.rows || []
    purch.pagination.rowsNumber = resp.total || 0
  } catch (e) {
    console.error('Failed to load purchases', e)
  } finally {
    purch.loading = false
  }
}

async function loadCapacity() {
  cap.loading = true
  try {
    const limit = cap.pagination.rowsPerPage
    const offset = (cap.pagination.page - 1) * cap.pagination.rowsPerPage
    const resp = await getPlanningResultCapacity(runId, {
      bucket_type: cap.filter.bucket_type,
      date_from: emptyToUndef(cap.filter.date_from),
      date_to: emptyToUndef(cap.filter.date_to),
      limit, offset
    })
    // attach a stable key
    cap.rows = (resp.rows || []).map((r: any, idx: number) => ({ key: `${r.area_id}-${r.bucket_type}-${r.bucket_date}-${idx}`, ...r }))
    cap.pagination.rowsNumber = resp.total || 0
  } catch (e) {
    console.error('Failed to load capacity', e)
  } finally {
    cap.loading = false
  }
}

async function loadPegging() {
  peg.loading = true
  try {
    const limit = peg.pagination.rowsPerPage
    const offset = (peg.pagination.page - 1) * peg.pagination.rowsPerPage
    const resp = await getPlanningResultPegging(runId, {
      child_item_id: peg.filter.child_item_id,
      parent_item_id: peg.filter.parent_item_id,
      date_from: emptyToUndef(peg.filter.date_from),
      date_to: emptyToUndef(peg.filter.date_to),
      limit, offset
    })
    peg.rows = resp.rows || []
    peg.pagination.rowsNumber = resp.total || 0
  } catch (e) {
    console.error('Failed to load pegging', e)
  } finally {
    peg.loading = false
  }
}

// --- Order Components helpers ---
function rebuildOrderOptions() {
  try {
    comp.orderOptions = (prod.rows || []).map((r: any) => {
      const label = `#${r.order_id} · item ${r.item_id} · qty ${fmt(r.qty)} · need ${r.need_date || ''}`
      return { label, value: Number(r.order_id) }
    })
  } catch {
    comp.orderOptions = []
  }
}

async function loadComponentsFromOrder() {
  const oid = Number(comp.selectedOrderId || 0)
  if (!oid) return
  const r = (prod.rows || []).find((x: any) => Number(x.order_id) === oid)
  if (!r) return
  comp.selectedItemId = Number(r.item_id)
  comp.selectedQty = Number(r.qty || 0)
  await fetchFullTree()
}

async function fetchFullTree() {
  const itemId = Number(comp.selectedItemId || 0)
  const qty = Number(comp.selectedQty || 0)
  if (!itemId || qty <= 0) return
  comp.loading = true
  try {
    const data = await getSpecificationFull({ item_id: itemId, root_qty: qty, max_depth: 50 })
    comp.rows = (data?.nodes || []) as SpecNode[]
  } catch (e) {
    console.error('Failed to load components tree', e)
  } finally {
    comp.loading = false
  }
}

function onProdRequest(ctx: any) {
  if (ctx?.pagination) prod.pagination = ctx.pagination
  loadProduction()
}
function onPurchRequest(ctx: any) {
  if (ctx?.pagination) purch.pagination = ctx.pagination
  loadPurchases()
}
function onCapRequest(ctx: any) {
  if (ctx?.pagination) cap.pagination = ctx.pagination
  loadCapacity()
}
function onPegRequest(ctx: any) {
  if (ctx?.pagination) peg.pagination = ctx.pagination
  loadPegging()
}

function emptyToUndef(s: string): string | undefined {
  const t = (s || '').trim()
  return t.length ? t : undefined
}

onMounted(async () => {
  await loadSummary()
  // Загружаем все данные параллельно
  await Promise.all([
    loadProduction(),
    loadPurchases(),
    loadDictionaries()
  ])
  // Догружаем недостающие записи словарей по item_id/area_id из фактических строк
  await fillMissingDictionariesFromRows()
  // Теперь, когда все данные загружены, вызываем группировку
  rebuildGroupedProductionOrders()
  try {
    console.log('MRP onMounted', {
      grouped: (groupedProdRows as any)?.value?.length ?? (groupedProductionOrders as any)?.value?.length ?? 0,
      prodRows: (prod.rows || []).length
    })
  } catch (e) {}
})

// Наблюдаем за вкладкой для загрузки данных при переключении
watch(tab, (t) => {
  if (t === 'production' && !prod.rows.length) loadProduction()
  if (t === 'purchases' && !purch.rows.length) loadPurchases()
  if (t === 'capacity') loadCapacity()
  if (t === 'pegging') loadPegging()
})

// Наблюдаем за изменениями prod.rows, itemMap и areaMap для обновления groupedProductionOrders
watch([() => prod.rows, () => itemMap.value, () => areaMap.value], () => {
  rebuildGroupedProductionOrders()
}, { deep: true })
</script>

<style scoped>
.text-h5 {
  font-weight: 600;
}
</style>