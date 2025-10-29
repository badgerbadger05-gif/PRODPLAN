<template>
  <q-page padding>
    <!-- Guards: error or loading -->
    <q-banner v-if="loadError" dense class="bg-red-2 text-negative q-mb-md">
      {{ t('mrp.errors.loadFailed') || 'Ошибка загрузки результатов' }}: {{ loadError }}
    </q-banner>
    <q-banner v-else-if="pageLoading || !summary" dense class="bg-grey-2 q-mb-md">
      <q-spinner color="primary" size="1.2em" class="q-mr-sm" /> {{ t('mrp.loading') || 'Загрузка результатов…' }}
    </q-banner>
    <!-- DIAG: remove after debugging -->
    <q-banner dense class="bg-blue-1 text-blue-9 q-mb-md">
      {{ diagText }}
    </q-banner>
    <div class="row items-center q-gutter-sm q-mb-md">
      <div class="text-h5">{{ t('mrp.title', { runId }) }}</div>
      <q-space />
      <q-chip v-if="summary?.run?.status" :color="statusColor(summary.run.status)" text-color="white" size="sm">
        {{ summary.run.status }}
      </q-chip>
    </div>

    <div class="q-mb-md">
      <MRPSummaryCard
        :run-id="runId"
        :summary="summary"
        @open-kind-issues="showKindIssuesDialog = true"
      />
    </div>

    <!-- Результаты прогона: две вкладки с едиными столбцами -->
    <div class="q-mb-md">
      <q-tabs v-model="viewTab" class="text-primary" dense>
        <q-tab name="production" icon="build" :label="t('mrp.tabs.production')" />
        <q-tab name="purchases" icon="shopping_cart" :label="t('mrp.tabs.purchases')" />
      </q-tabs>
      <q-separator />

      <q-tab-panels v-model="viewTab" animated>
        <!-- Production unified tab -->
        <q-tab-panel name="production">
          <ProductionFilters
            v-model="prod.filter"
            :loading="prod.loading"
            :title="`Результаты MRP от ${summary?.run?.started_at || '—'}`"
            :show-day-picker="false"
            @apply="applyProdFiltersDebounced()"
            @reset="resetFilters"
          >
            <template #extra-actions>
              <q-separator vertical class="q-mx-xs" />
              <q-btn dense flat icon="download" :label="t('mrp.actions.csv')" @click="exportProd('csv')" />
              <q-btn dense flat icon="table_view" :label="t('mrp.actions.xlsx')" @click="exportProd('xlsx')" />
              <q-separator vertical class="q-mx-xs" />
              <q-btn dense flat icon="warning" color="negative" :label="t('mrp.actions.shortageReport')" @click="exportShortageReport" />
            </template>
          </ProductionFilters>

          <!-- Группированный вывод по видам производства -->
          <template v-if="groupedProdRows.length">
            <ProductionGroupedTable
              :groups="groupedProdRows"
              :loading="prod.loading"
            />
          </template>

          <!-- Фолбэк: плоский список без группировки -->
          <template v-else>
            <ProductionUnifiedTable
              :rows="plainProdRows"
              :loading="prod.loading"
            />
          </template>
        </q-tab-panel>

        <!-- Purchases unified tab -->
        <q-tab-panel name="purchases">
          <ProductionFilters
            v-model="purch.filter"
            :loading="purch.loading"
            :title="`Результаты MRP от ${summary?.run?.started_at || '—'}`"
            :show-day-picker="false"
            @apply="applyPurchFiltersDebounced()"
            @reset="onPurchReset"
          >
            <template #extra-actions>
              <q-separator vertical class="q-mx-xs" />
              <q-btn dense flat icon="download" :label="t('mrp.actions.csv')" @click="exportPurch('csv')" />
              <q-btn dense flat icon="table_view" :label="t('mrp.actions.xlsx')" @click="exportPurch('xlsx')" />
            </template>
          </ProductionFilters>

          <PurchasesUnifiedTable
            :rows="purchAggRows"
            :loading="purch.loading"
          />
        </q-tab-panel>
      </q-tab-panels>
    </div>

    <!-- Вкладки для детального анализа (можно оставить ниже) -->
    <q-separator class="q-my-lg" />
    <div class="text-h6 q-mb-md">{{ t('mrp.sections.detail') }}</div>
    <q-tabs v-model="tab" class="text-primary q-mb-sm" dense>
      <q-tab name="production" icon="build" :label="t('mrp.tabs.productionDetail')" />
      <q-tab name="purchases" icon="shopping_cart" :label="t('mrp.tabs.purchasesDetail')" />
      <q-tab name="capacity" icon="bar_chart" :label="t('mrp.tabs.capacity')" />
      <q-tab name="pegging" icon="device_hub" :label="t('mrp.tabs.pegging')" />
      <q-tab name="components" icon="list" :label="t('mrp.tabs.components')" />
    </q-tabs>
    <q-separator />

    <q-tab-panels v-model="tab" animated>
      <!-- Production -->
      <q-tab-panel name="production">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-input v-model="prod.filter.date_from" dense outlined :label="t('mrp.filters.fromDate')" style="width: 200px" />
          <q-input v-model="prod.filter.date_to" dense outlined :label="t('mrp.filters.toDate')" style="width: 200px" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="applyProdFilters()" />
        </div>
        <ProductionDetailTable
          :rows="prod.rows"
          :columns="prod.columns"
          :loading="prod.loading"
          :pagination="prod.pagination"
          @request="onProdRequest"
        />
      </q-tab-panel>

      <!-- Purchases -->
      <q-tab-panel name="purchases">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-input v-model="purch.filter.date_from" dense outlined :label="t('mrp.filters.fromDate')" style="width: 200px" />
          <q-input v-model="purch.filter.date_to" dense outlined :label="t('mrp.filters.toDate')" style="width: 200px" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="applyPurchFiltersDebounced()" />
        </div>
        <PurchasesDetailTable
          :rows="purch.rows"
          :columns="purch.columns"
          :loading="purch.loading"
          :pagination="purch.pagination"
          @request="onPurchRequest"
        />
      </q-tab-panel>

      <!-- Capacity -->
      <q-tab-panel name="capacity">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-input v-model="cap.filter.date_from" dense outlined :label="t('mrp.filters.fromDate')" style="width: 200px" />
          <q-input v-model="cap.filter.date_to" dense outlined :label="t('mrp.filters.toDate')" style="width: 200px" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadCapacity()" />
        </div>
        <CapacityTable
          :rows="cap.rows"
          :columns="cap.columns"
          :loading="cap.loading"
          :pagination="cap.pagination"
          @request="onCapRequest"
        />
      </q-tab-panel>

      <!-- Pegging -->
      <q-tab-panel name="pegging">
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-input v-model.number="peg.filter.child_item_id" type="number" dense outlined :label="t('mrp.pegging.filters.childItemId')" style="width: 160px" />
          <q-input v-model.number="peg.filter.parent_item_id" type="number" dense outlined :label="t('mrp.pegging.filters.parentItemId')" style="width: 160px" />
          <q-input v-model="peg.filter.date_from" dense outlined :label="t('mrp.filters.fromDate')" style="width: 200px" />
          <q-input v-model="peg.filter.date_to" dense outlined :label="t('mrp.filters.toDate')" style="width: 200px" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadPegging()" />
        </div>
        <PeggingTable
          :rows="peg.rows"
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
            :label="t('mrp.components.selectOrder')"
            style="min-width: 360px"
          />
          <q-btn dense color="primary" icon="visibility" :label="t('mrp.actions.showByOrder')" @click="loadComponentsFromOrder" />

          <q-separator vertical inset class="q-mx-sm" />

          <q-input v-model.number="comp.selectedItemId" type="number" dense outlined label="item_id" style="width: 150px" />
          <q-input v-model.number="comp.selectedQty" type="number" dense outlined label="qty" style="width: 120px" />
          <q-btn dense color="primary" icon="search" :label="t('mrp.actions.show')" @click="fetchFullTree" />
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
                <span v-else-if="col.name === 'article'">{{ props.row.article || t('mrp.placeholder.noArticle') }}</span>
                <span v-else-if="col.name === 'qty'">{{ fmt(props.row.computed?.treeQty ?? 0) }}</span>
                <span v-else-if="col.name === 'stage'">{{ props.row.stage ? (props.row.stage?.name || props.row.stage?.id) : t('mrp.placeholder.noArticle') }}</span>
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
                        <span v-else-if="col.name === 'article'">{{ childProps.row.article || t('mrp.placeholder.noArticle') }}</span>
                        <span v-else-if="col.name === 'qty'">{{ fmt(childProps.row.computed?.treeQty ?? 0) }}</span>
                        <span v-else-if="col.name === 'stage'">{{ childProps.row.stage ? (childProps.row.stage?.name || childProps.row.stage?.id) : t('mrp.placeholder.noArticle') }}</span>
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
    <!-- Диалог: проблемы привязки видов производства -->
    <KindIssuesDialog v-model="showKindIssuesDialog" :issues="kindIssuesRows" />
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
  listResources,
  exportPlanningResultProduction,
  exportPlanningResultPurchases,
  getPlanningResultProductionGrouped,
  getPlanningResultPurchasesGrouped,
  getPlanningResultCapacitySummary,
  getShortageReport
} from '../services/api'
import type { QTableColumn } from 'quasar'
import type { SpecNode } from '../services/api'
import type { ProductionOrder, PurchaseRow, CapacityRow, PeggingRow } from '../types/mrp'
import { useFormatting } from '../composables/useFormatting'
import MRPSummaryCard from '../components/mrp/MRPSummaryCard.vue'
import ProductionFilters from '../components/mrp/ProductionFilters.vue'
import ProductionUnifiedTable from '../components/mrp/ProductionUnifiedTable.vue'
import PurchasesUnifiedTable from '../components/mrp/PurchasesUnifiedTable.vue'
import ProductionGroupedTable from '../components/mrp/ProductionGroupedTable.vue'
import CapacityTable from '../components/mrp/CapacityTable.vue'
import PeggingTable from '../components/mrp/PeggingTable.vue'
import KindIssuesDialog from '../components/mrp/KindIssuesDialog.vue'
import { useI18n } from 'vue-i18n'

// Safe i18n accessor to avoid NOT_INSTALLED crash during route mount
let _i18nInst: any = null
try {
  _i18nInst = useI18n()
} catch (e) {
  console.error('useI18n NOT INSTALLED yet (fallback in use).', e)
}
const t = ((key: any, params?: any) => {
  try {
    return _i18nInst?.t ? _i18nInst.t(key as any, params as any) : String(key)
  } catch {
    return String(key)
  }
}) as any

// --- Справочники (должны быть объявлены ДО использования в колонках) ---
const itemMap = ref<{ [key: number]: any }>({})
const areaMap = ref<{ [key: number]: string }>({})

const prodColumns: QTableColumn<ProductionOrder>[] = [
  { name: 'order_id', label: t('mrp.columns.orderId'), field: 'order_id', align: 'left', sortable: true },
  { name: 'item_id', label: t('mrp.columns.itemId'), field: 'item_id', align: 'right', sortable: true },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right', sortable: true },
  { name: 'norm_hours_per_unit', label: t('mrp.columns.normPerUnit'), field: 'norm_hours_per_unit', align: 'right', sortable: true },
  { name: 'norm_hours_total', label: t('mrp.columns.normTotal'), field: 'norm_hours_total', align: 'right', sortable: true },
  { name: 'need_date', label: t('mrp.columns.needDate'), field: 'need_date', align: 'left', sortable: true },
  { name: 'start_date', label: t('mrp.columns.startDate'), field: 'start_date', align: 'left', sortable: true },
  { name: 'finish_date', label: t('mrp.columns.finishDate'), field: 'finish_date', align: 'left', sortable: true },
  { name: 'bucket_type', label: t('mrp.columns.bucketType'), field: 'bucket_type', align: 'left', sortable: true },
  { name: 'bucket_date', label: t('mrp.columns.bucketDate'), field: 'bucket_date', align: 'left', sortable: true },
  { name: 'priority_index', label: t('mrp.columns.priorityIndex'), field: 'priority_index', align: 'right', sortable: true },
  { name: 'stages', label: t('mrp.columns.stages'), field: 'stages', align: 'left' }
]

const recommendedProdColumns: QTableColumn<any>[] = [
  { name: 'item_name', label: t('mrp.columns.name'), field: 'item_name', align: 'left' },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right' },
  { name: 'need_date', label: t('mrp.columns.needDate'), field: 'need_date', align: 'left' }
];

const recommendedPurchColumns: QTableColumn<any>[] = [
  { name: 'item_name', label: t('mrp.columns.name'), field: (r: any) => (itemMap.value?.[r.item_id]?.item_name) ?? t('mrp.placeholder.itemNameFallback', { id: r.item_id }), align: 'left', sortable: true },
  { name: 'item_article', label: t('mrp.columns.article'), field: (r: any) => (itemMap.value?.[r.item_id]?.item_article) ?? t('mrp.placeholder.noArticle'), align: 'left', sortable: true },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right', sortable: true, format: (val) => fmt(val) },
  { name: 'need_date', label: t('mrp.columns.needDate'), field: 'need_date', align: 'left', sortable: true },
  { name: 'order_date', label: t('mrp.columns.orderDate'), field: 'order_date', align: 'left', sortable: true }
];

const purchColumns: QTableColumn<PurchaseRow>[] = [
  { name: 'purchase_id', label: t('mrp.columns.purchaseId'), field: 'purchase_id', align: 'left', sortable: true },
  { name: 'item_id', label: t('mrp.columns.itemId'), field: 'item_id', align: 'right', sortable: true },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right', sortable: true },
  { name: 'need_date', label: t('mrp.columns.needDate'), field: 'need_date', align: 'left', sortable: true },
  { name: 'order_date', label: t('mrp.columns.orderDate'), field: 'order_date', align: 'left', sortable: true },
  { name: 'lead_time_days', label: t('mrp.columns.leadTimeDays'), field: 'lead_time_days', align: 'right', sortable: true },
  { name: 'bucket_type', label: t('mrp.columns.bucketType'), field: 'bucket_type', align: 'left', sortable: true },
  { name: 'bucket_date', label: t('mrp.columns.bucketDate'), field: 'bucket_date', align: 'left', sortable: true },
  { name: 'priority_index', label: t('mrp.columns.priorityIndex'), field: 'priority_index', align: 'right', sortable: true }
]

const capColumns: QTableColumn<CapacityRow>[] = [
  { name: 'area_id', label: t('mrp.columns.areaId'), field: 'area_id', align: 'right', sortable: true },
  { name: 'bucket_type', label: t('mrp.columns.bucketType'), field: 'bucket_type', align: 'left', sortable: true },
  { name: 'bucket_date', label: t('mrp.columns.bucketDate'), field: 'bucket_date', align: 'left', sortable: true },
  { name: 'hours_planned', label: t('mrp.columns.hoursPlanned'), field: 'hours_planned', align: 'right', sortable: true },
  { name: 'hours_available', label: t('mrp.columns.hoursAvailable'), field: 'hours_available', align: 'right', sortable: true },
  { name: 'overload_hours', label: t('mrp.columns.overloadHours'), field: 'overload_hours', align: 'right', sortable: true }
]

/** Pegging columns are defined inside PeggingTable component */

// Унифицированные колонки для вкладок «Производство» и «Закупки»
const prodUnifiedColumns: QTableColumn<any>[] = [
  { name: 'name', label: t('mrp.columns.name'), field: 'item_name', align: 'left' },
  { name: 'article', label: t('mrp.columns.article'), field: 'item_article', align: 'left' },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right' },
  { name: 'norm_per_unit', label: t('mrp.columns.normPerUnit'), field: 'norm_hours_per_unit', align: 'right' },
  { name: 'norm_total', label: t('mrp.columns.normTotal'), field: 'norm_hours_total', align: 'right' }
]

const purchUnifiedColumns: QTableColumn<any>[] = [
  { name: 'name', label: t('mrp.columns.name'), field: 'item_name', align: 'left' },
  { name: 'article', label: t('mrp.columns.article'), field: 'item_article', align: 'left' },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right' }
]

const route = useRoute()
const runId = Number(route.params.runId)
try { console.log('MRPResultPage diag', { params: route.params, runId }) } catch {}
const diagText = computed(() => {
  try {
    return `diag: runId=${runId} · params=${JSON.stringify(route.params)}`
  } catch {
    return `diag: runId=${runId} · params=[unserializable]`
  }
})

const summary = ref<any | null>(null)
const pageLoading = ref(true)
const loadError = ref<string | null>(null)
const tab = ref<'production' | 'purchases' | 'capacity' | 'pegging' | 'components'>('production')
// Вкладки верхнего уровня для унифицированных таблиц
const viewTab = ref<'production' | 'purchases'>('production')

// ---- Диагностика проблем привязки видов производства ----
const showKindIssuesDialog = ref(false)
const kindIssues = computed(() => {
  const arr = (summary.value?.warnings || []) as any[]
  return arr.filter((w: any) =>
    String(w?.code || '') === 'NO_AREA_FOR_PRODUCTION_KIND' ||
    String(w?.code || '') === 'NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM'
  )
})
const kindIssuesRows = computed(() => {
  return (kindIssues.value || []).map((w: any, idx: number) => ({
    key: idx,
    production_kind_id: w?.production_kind_id ?? null,
    production_kind_name: w?.production_kind_name ?? null,
    item_id: w?.item_id ?? null,
    item_code: w?.item_code ?? null,
    item_name: w?.item_name ?? null,
    item_article: w?.item_article ?? null,
    root_item_id: w?.root_item_id ?? null,
    root_item_code: w?.root_item_code ?? null,
    root_item_name: w?.root_item_name ?? null,
    root_item_article: w?.root_item_article ?? null,
    spec_id: w?.spec_id ?? null,
    spec_ref1c: w?.spec_ref1c ?? null,
    spec_code: w?.spec_code ?? null,
    spec_name: w?.spec_name ?? null,
    code: w?.code ?? ''
  }))
})

// Popup флаг для выбора даты «День задания»

 // --- Справочники (объявлены выше) ---
 
 // --- Группировка для новых таблиц ---
 const groupedProductionOrders = ref<any[]>([])
 // Полные наборы строк для верхних таблиц (без учёта пагинации детальных)
 const prodAllRows = ref<any[]>([])
 const purchAllRows = ref<any[]>([])
 const purchGroupedRows = ref<any[]>([])
// Итоговый источник строк для карточки «Рекомендуемые заказы на производство»
const groupedProdRows = computed(() => {
  return groupedProductionOrders.value || []
})
// Плоский список для фолбэка
const plainProdRows = computed(() => {
 const src = prodAllRows.value || []
  
  // Create a map to deduplicate rows based on agg_key or a combination of item_id and unit
 const rowMap = new Map<string, any>()
  
  src.forEach((r: any) => {
    const key = r.agg_key || `${r.item_id}|${r.unit || ''}`
    // If row with same key already exists, merge/accumulate values as needed
    if (rowMap.has(key)) {
      const existingRow = rowMap.get(key)!
      // Accumulate quantities and norm hours when duplicates found
      rowMap.set(key, {
        ...r,
        item_name: (itemMap.value?.[r.item_id]?.item_name) ?? t('mrp.placeholder.itemNameFallback', { id: r.item_id }),
        item_article: (itemMap.value?.[r.item_id]?.item_article) ?? t('mrp.placeholder.noArticle'),
        qty: (existingRow.qty || 0) + (r.qty || 0),
        norm_hours_total: (existingRow.norm_hours_total || 0) + (r.norm_hours_total || 0)
      })
    } else {
      rowMap.set(key, {
        ...r,
        item_name: (itemMap.value?.[r.item_id]?.item_name) ?? t('mrp.placeholder.itemNameFallback', { id: r.item_id }),
        item_article: (itemMap.value?.[r.item_id]?.item_article) ?? t('mrp.placeholder.noArticle')
      })
    }
  })
  
  return Array.from(rowMap.values())
})
// Агрегация закупок по item_id+unit для верхней вкладки (независимо от пагинации детальных)
const purchAggRows = computed(() => {
  return purchGroupedRows.value || []
})


async function rebuildGroupedProductionOrders() {
  try {
    // При однодневном диапазоне (когда date_from и date_to одинаковы) бэкенд может возвращать данные по-разному
    // Поэтому явно проверим и обработаем этот случай
    const dateFrom = emptyToUndef(prod.filter.date_from)
    const dateTo = emptyToUndef(prod.filter.date_to)
    
    // Если даты одинаковы, убедимся, что бэкенд получает корректные параметры
    const params = {
      date_from: dateFrom,
      date_to: dateTo,
      limit: 1000,
      offset: 0,
      sort_by: 'item_name' as const,
      sort_dir: 'asc' as const
    }
    
    const resp = await getPlanningResultProductionGrouped(runId, params)
    const groups = (resp?.groups || []).map((g: any) => ({
      area_id: g.area_id,
      area_name: g.area_name,
      orders: g.orders || [],
      norm_sum_hours: Number(g.norm_sum_hours || 0),
      min_days_to_need: (g.min_days_to_need != null) ? Number(g.min_days_to_need) : null,
      cap_overload_hours: Number(g.cap_overload_hours || 0),
      cap_overloaded_buckets: Number(g.cap_overloaded_buckets || 0)
    }))
    groupedProductionOrders.value = groups
  } catch (e) {
    console.error('Failed to load grouped production', e)
    groupedProductionOrders.value = []
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

    // Из production (полный набор)
    for (const r of (prodAllRows.value || [])) {
      if (r?.item_id && !itemMap.value[r.item_id]) missingItemIds.add(Number(r.item_id))
      const stages = Array.isArray(r?.stages) ? r.stages : []
      for (const s of stages) {
        const aid = s?.area_id
        if (aid && !areaMap.value[aid]) missingAreaIds.add(Number(aid))
      }
    }
    // Из purchases (полный набор)
    for (const r of (purchAllRows.value || [])) {
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

const bucketOptions = computed(() => ([
  { label: t('mrp.filters.bucketOption.any'), value: undefined },
  { label: t('mrp.filters.bucketOption.daily'), value: 'daily' },
  { label: t('mrp.filters.bucketOption.weekly'), value: 'weekly' }
]))

// Production state
const prod = reactive<{
  rows: ProductionOrder[]
  loading: boolean
  filter: { date_from: string; date_to: string }
  pagination: { page: number; rowsPerPage: number; rowsNumber: number }
  columns: QTableColumn<ProductionOrder>[]
}>({
  rows: [] as ProductionOrder[],
  loading: false,
  filter: { date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 20, rowsNumber: 0 },
  columns: prodColumns
})

// Purchases state
const purch = reactive<{
  rows: PurchaseRow[]
  loading: boolean
  filter: { date_from: string; date_to: string }
  pagination: { page: number; rowsPerPage: number; rowsNumber: number }
  columns: QTableColumn<PurchaseRow>[]
}>({
  rows: [] as PurchaseRow[],
  loading: false,
  filter: { date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 20, rowsNumber: 0 },
  columns: purchColumns
})

// Capacity state
const cap = reactive<{
  rows: CapacityRow[]
  loading: boolean
  filter: { date_from: string; date_to: string }
  pagination: { page: number; rowsPerPage: number; rowsNumber: number }
  columns: QTableColumn<CapacityRow>[]
}>({
  rows: [] as CapacityRow[],
  loading: false,
  filter: { date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 30, rowsNumber: 0 },
  columns: capColumns
})

// Pegging state
const peg = reactive<{
  rows: PeggingRow[]
  loading: boolean
  filter: { child_item_id?: number; parent_item_id?: number; date_from: string; date_to: string }
  pagination: { page: number; rowsPerPage: number; rowsNumber: number }
}>({
  rows: [] as PeggingRow[],
  loading: false,
  filter: { child_item_id: undefined, parent_item_id: undefined, date_from: '', date_to: '' },
  pagination: { page: 1, rowsPerPage: 30, rowsNumber: 0 }
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
    { name: 'name', label: t('mrp.components.columns.name'), field: 'name', align: 'left', sortable: true },
    { name: 'article', label: t('mrp.columns.article'), field: 'article', align: 'left', sortable: true },
    { name: 'qty', label: t('mrp.components.columns.requiredQty'), field: (r: SpecNode) => (r as any)?.computed?.treeQty ?? 0, align: 'right', sortable: true },
    { name: 'stage', label: t('mrp.components.columns.stage'), field: (r: SpecNode) => (r?.stage ? (r.stage as any).name || (r.stage as any).id : null), align: 'left' }
  ] as QTableColumn<SpecNode>[]
})

const { formatNumber: fmt, formatQty: fmtQty, statusColor, warnText } = useFormatting()

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
    
    // Проверяем, если диапазон дат состоит из одного дня
    const dateFrom = emptyToUndef(prod.filter.date_from)
    const dateTo = emptyToUndef(prod.filter.date_to)
    
    console.log('loadProduction called with filters:', {
      date_from: dateFrom,
      date_to: dateTo,
      runId: runId
    });
    
    const resp = await getPlanningResultProduction(runId, {
      date_from: dateFrom,
      date_to: dateTo,
      sort_by: 'item_name' as const,
      sort_dir: 'asc' as const,
      limit, offset
    })
    
    console.log('loadProduction response:', {
      total: resp.total,
      rowsCount: resp.rows?.length || 0,
      rows: resp.rows || []
    });
    
    prod.rows = resp.rows || []
    prod.pagination.rowsNumber = resp.total || 0
    // Отказываемся от полной выгрузки 10000 строк — используем только текущую страницу как «полный» источник для фолбэка
    prodAllRows.value = (resp?.rows || [])
    rebuildOrderOptions()
    // Пересобираем группы на сервере
    rebuildGroupedProductionOrders()
    // Индикаторы capacity для верхнего агрегата (по текущим фильтрам)
    await loadCapacityUpper()
    // Ежедневная повестка + мощность за конкретный день (если выбран)
 } catch (e) {
     console.error('Failed to load production', e)
     // В случае ошибки очищаем данные
     prod.rows = []
     prod.pagination.rowsNumber = 0
     prodAllRows.value = []
   } finally {
     prod.loading = false
  }
}

async function loadPurchases() {
  purch.loading = true
  try {
    const limit = purch.pagination.rowsPerPage
    const offset = (purch.pagination.page - 1) * purch.pagination.rowsPerPage
    const [resp, grouped] = await Promise.all([
      getPlanningResultPurchases(runId, {
        date_from: emptyToUndef(purch.filter.date_from),
        date_to: emptyToUndef(purch.filter.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc',
        limit, offset
      }),
      getPlanningResultPurchasesGrouped(runId, {
        date_from: emptyToUndef(purch.filter.date_from),
        date_to: emptyToUndef(purch.filter.date_to),
        limit: 1000,
        offset: 0
      })
    ])
    purch.rows = resp.rows || []
    purch.pagination.rowsNumber = resp.total || 0
    // Отказ от полной выгрузки 100000 строк — используем текущую страницу как «полный» источник для фолбэка
    purchAllRows.value = (resp?.rows || [])
    purchGroupedRows.value = (grouped?.rows || [])
  } catch (e) {
    console.error('Failed to load purchases', e)
  } finally {
    purch.loading = false
  }
}

// --- Export helpers and actions ---
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
  }
}

async function exportProd(fmt: 'csv' | 'xlsx') {
  try {
    const params: any = {
      format: fmt,
      date_from: emptyToUndef(prod.filter.date_from),
      date_to: emptyToUndef(prod.filter.date_to),
      sort_by: 'need_date',
      sort_dir: 'asc'
    }
    const res = await exportPlanningResultProduction(runId, params)
    if (fmt === 'csv') {
      downloadTextFile(res?.data || '', res?.filename || `mrp_production_run_${runId}.csv`, 'text/csv;charset=utf-8')
    } else {
      downloadBase64Xlsx(res?.data_base64 || '', res?.filename || `mrp_production_run_${runId}.xlsx`)
    }
  } catch (e) {
    console.error('Export production failed', e)
  }
}

async function exportPurch(fmt: 'csv' | 'xlsx') {
  try {
    const res = await exportPlanningResultPurchases(runId, {
      format: fmt,
      date_from: emptyToUndef(purch.filter.date_from),
      date_to: emptyToUndef(purch.filter.date_to),
      sort_by: 'item_name',
      sort_dir: 'asc'
    })
    if (fmt === 'csv') {
      downloadTextFile(res?.data || '', res?.filename || `mrp_purchases_run_${runId}.csv`, 'text/csv;charset=utf-8')
    } else {
      downloadBase64Xlsx(res?.data_base64 || '', res?.filename || `mrp_purchases_run_${runId}.xlsx`)
    }
  } catch (e) {
    console.error('Export purchases failed', e)
  }
}

async function exportShortageReport() {
  try {
    const res = await getShortageReport(runId)
    if (res.status === 'ok' && res.total_rows === 0) {
      alert(t('mrp.messages.noShortages') || 'Дефицитов не найдено')
      return
    }
    if (res?.data_base64) {
      downloadBase64Xlsx(res.data_base64, res.filename || `mrp_shortage_report_run_${runId}.xlsx`)
    } else {
      const message = t('mrp.errors.shortageReportFailed')
      alert(String(message))
    }
 } catch (e: any) {
    console.error('Export shortage report failed', e)
    const detail =
      (e?.response?.data?.detail as any) ||
      (e?.message as any) ||
      t('mrp.errors.shortageReportFailed')
    alert(String(detail))
  }
}

async function loadCapacity() {
  cap.loading = true
  try {
    const limit = cap.pagination.rowsPerPage
    const offset = (cap.pagination.page - 1) * cap.pagination.rowsPerPage
    const resp = await getPlanningResultCapacity(runId, {
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

// Карта capacity для верхнего агрегата (перегруз по участкам в выбранном периоде)
const capUpper = ref<{ [areaId: number]: { overload_hours: number; hours_planned: number; hours_available: number; overloaded_buckets: number } }>({})
 
async function loadCapacityUpper() {
  try {
    const resp = await getPlanningResultCapacitySummary(runId, {
      date_from: emptyToUndef(prod.filter.date_from),
      date_to: emptyToUndef(prod.filter.date_to)
    })
    const map: { [k: number]: { overload_hours: number; hours_planned: number; hours_available: number; overloaded_buckets: number } } = {}
    const m = (resp?.map || {}) as any
    for (const k of Object.keys(m)) {
      const aid = Number(k)
      const v = m[k] || {}
      map[aid] = {
        overload_hours: Number(v.overload_hours || 0),
        hours_planned: Number(v.hours_planned || 0),
        hours_available: Number(v.hours_available || 0),
        overloaded_buckets: Number(v.overloaded_buckets || 0)
      }
    }
    capUpper.value = map
  } catch (e) {
    console.error('Failed to load capacity for upper indicators', e)
  }
  // После обновления карты мощностей — пересобираем агрегаты, чтобы появились индикаторы перегруза
  rebuildGroupedProductionOrders()
}

// Capacity для выбранного дня (daily)
 

// Сброс фильтров (день, бакет, даты) и перезагрузка данных
function resetFilters() {
  // Очистка фильтров
  prod.filter.date_from = ''
  prod.filter.date_to = ''
  // Перезагрузка наборов
  loadProduction()
  loadPurchases()
  loadCapacityUpper()
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
function onPurchReset() {
  purch.filter.date_from = ''
  purch.filter.date_to = ''
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

// --- Debounce helpers and debounced actions ---
function debounce<T extends (...args: any[]) => any>(fn: T, ms = 250) {
  let timer: number | undefined
  return (...args: Parameters<T>) => {
    if (timer) clearTimeout(timer as any)
    timer = window.setTimeout(() => fn(...args), ms)
  }
}

// Новая функция для сбора параметров фильтров
function collectFilterParams() {
  // Собираем параметры для production фильтров
  const prodParams = {
    date_from: emptyToUndef(prod.filter.date_from),
    date_to: emptyToUndef(prod.filter.date_to),
  }
  
  // Собираем параметры для purchases фильтров
  const purchParams = {
    date_from: emptyToUndef(purch.filter.date_from),
    date_to: emptyToUndef(purch.filter.date_to)
  }
  
  // Собираем параметры для capacity фильтров
 const capParams = {
    date_from: emptyToUndef(cap.filter.date_from),
    date_to: emptyToUndef(cap.filter.date_to)
  }
  
 // Собираем параметры для pegging фильтров
  const pegParams = {
    child_item_id: peg.filter.child_item_id,
    parent_item_id: peg.filter.parent_item_id,
    date_from: emptyToUndef(peg.filter.date_from),
    date_to: emptyToUndef(peg.filter.date_to)
  }
  
  return {
    prod: prodParams,
    purch: purchParams,
    cap: capParams,
    peg: pegParams
 }
}

const applyProdFilters = async () => {
  // Собираем актуальные параметры фильтров
  const filterParams = collectFilterParams();
  const prodFilters = filterParams.prod;
  
  // Перед загрузкой данных убедимся, что фильтры корректны
  // Если даты одинаковы, это однодневный диапазон
  
  // Добавляем отладочную информацию
  console.log('applyProdFilters called with filters:', prodFilters);
  
  // Обновляем фильтры в prod.filter для согласованности
  prod.filter.date_from = prodFilters.date_from || '';
  prod.filter.date_to = prodFilters.date_to || '';
  await loadProduction()
  await loadCapacityUpper()
}

const applyProdFiltersDebounced = debounce(applyProdFilters, 250)

// Добавляем функцию для проверки состояния фильтров
function debugProdFilters() {
  console.log('Current prod filters state:', { ...prod.filter });
  console.log('Production rows count:', prod.rows.length);
  console.log('Production all rows count:', prodAllRows.value.length);
}
const applyPurchFiltersDebounced = debounce(loadPurchases, 250)
const loadCapacityUpperDebounced = debounce(loadCapacityUpper, 250)

onMounted(async () => {
  console.time('MRPResultPage:onMounted')
  try {
    await loadSummary()
    console.time('MRP:loaders')
    await Promise.all([
      loadProduction(),
      loadPurchases(),
      loadDictionaries()
    ])
    console.timeEnd('MRP:loaders')
    // Догружаем недостающие записи словарей по item_id/area_id из фактических строк
    await fillMissingDictionariesFromRows()
    // Теперь, когда все данные загружены, вызываем агрегаты
    rebuildGroupedProductionOrders()
    console.log('MRP onMounted', {
      grouped: (groupedProductionOrders as any)?.value?.length ?? 0,
      prodRows: (prod.rows || []).length,
      purchRows: (purch.rows || []).length
    })
  } catch (e: any) {
    console.error('MRPResultPage mount error', e)
    try {
      loadError.value = e?.message ? String(e.message) : JSON.stringify(e)
    } catch {
      loadError.value = String(e)
    }
  } finally {
    pageLoading.value = false
    console.timeEnd('MRPResultPage:onMounted')
  }
})

// Наблюдаем за вкладкой для загрузки данных при переключении
watch(tab, (t) => {
  if (t === 'production' && !prod.rows.length) loadProduction()
  if (t === 'purchases' && !purch.rows.length) loadPurchases()
  if (t === 'capacity') loadCapacity()
  if (t === 'pegging') loadPegging()
})

// Автозагрузка при переключении верхних вкладок
watch(viewTab, (vt) => {
  if (vt === 'production' && !prod.rows.length) loadProduction()
  if (vt === 'purchases' && !purch.rows.length) loadPurchases()
})

// Актуализируем индикаторы перегруза при изменении фильтров верхней вкладки «Производство»
watch([() => prod.filter.date_from, () => prod.filter.date_to], () => {
  loadCapacityUpperDebounced()
})
</script>

<style scoped>
.text-h5 {
  font-weight: 600;
}

/* Компактные строки таблиц: ~в 2 раза меньше высоты */
.compact-rows .q-td,
.compact-rows .q-th {
  padding: 4px 8px;   /* уменьшенные отступы по вертикали */
  line-height: 1.1;
  font-size: 12px;    /* компактный шрифт для плотности */
}

/* Чуть сжать содержимое ячеек с числами */
.compact-rows .q-td.text-right {
  padding-right: 8px;
}
</style>