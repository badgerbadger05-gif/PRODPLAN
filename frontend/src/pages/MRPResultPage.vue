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

    <!-- Результаты прогона: две вкладки с едиными столбцами -->
    <div class="q-mb-md">
      <q-tabs v-model="viewTab" class="text-primary" dense>
        <q-tab name="production" icon="build" label="Заказы на производство" />
        <q-tab name="purchases" icon="shopping_cart" label="Заказы на закупку" />
      </q-tabs>
      <q-separator />

      <q-tab-panels v-model="viewTab" animated>
        <!-- Production unified tab -->
        <q-tab-panel name="production">
          <div class="row items-center q-gutter-sm q-mb-sm">
            <div class="text-subtitle2">
              Результаты MRP от {{ summary?.run?.started_at || '—' }}
            </div>
            <q-space />
            <q-select v-model="prod.filter.bucket_type" :options="bucketOptions" emit-value map-options dense outlined label="Бакет" style="width: 150px" />
            <q-input v-model="prod.filter.day_date" dense outlined label="День задания (YYYY-MM-DD)" style="width: 200px">
              <template v-slot:append>
                <q-btn dense flat round icon="event" @click.stop="showDayMenu = true" />
                <q-menu v-model="showDayMenu" anchor="bottom right" self="top right" cover>
                  <q-date v-model="prod.filter.day_date" mask="YYYY-MM-DD" @update:model-value="onDayPicked" />
                </q-menu>
              </template>
            </q-input>
            <q-separator vertical class="q-mx-xs" />
            <q-input v-model="prod.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
            <q-input v-model="prod.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
            <q-btn dense color="primary" icon="search" @click="loadProduction()" />
            <q-btn dense flat icon="refresh" @click="loadProduction()" />
            <q-separator vertical class="q-mx-xs" />
            <q-btn dense flat icon="download" label="CSV" @click="exportProd('csv')" />
            <q-btn dense flat icon="table_view" label="XLSX" @click="exportProd('xlsx')" />
          </div>

          <!-- Ежедневное задание по видам производства (если выбран день) -->
          <template v-if="prod.filter.day_date && dailyAgendaGroups.length">
            <q-table
              dense
              table-class="compact-rows"
              :rows="dailyAgendaGroups"
              :columns="prodUnifiedColumns"
              row-key="area_id"
              :loading="prod.loading"
              :pagination="{ rowsPerPage: 50 }"
              hide-header
            >
              <template v-slot:body="props">
                <!-- Заголовок группы (дневная повестка) -->
                <q-tr :props="props" :key="`grp_day_${props.row.area_id}`">
                  <q-td colspan="100%" class="bg-grey-2">
                    <div class="text-subtitle1">
                      <strong>Вид производства:</strong> {{ props.row.area_name }}
                      <span class="text-grey q-ml-sm">
                        · Позиции (на день): {{ (props.row.orders || []).length }}
                        · Норматив (за день): {{ fmt(props.row.norm_sum_hours) }} ч
                        · Выпуск (за день): {{ fmtQty(props.row.sum_qty, 'шт') }}
                      </span>
                      <q-badge v-if="Number(props.row.cap_overload_hours || 0) > 0" class="q-ml-sm" color="negative" outline>
                        Перегруз: {{ fmt(props.row.cap_overload_hours) }} ч
                      </q-badge>
                    </div>
                  </q-td>
                </q-tr>
                <!-- Строки позиций на день -->
                <q-tr v-for="order in props.row.orders" :key="order.agg_key || `${order.item_id}|${order.unit || ''}`" :props="props">
                  <q-td key="name" :props="props">
                    <div>{{ order.item_name || ('Номенклатура #' + order.item_id) }}</div>
                    <q-badge v-if="!(Number(order.norm_hours_per_unit || 0) > 0)" class="q-ml-xs" color="grey" outline>без норматива</q-badge>
                  </q-td>
                  <q-td key="article" :props="props">
                    {{ order.item_article || '—' }}
                  </q-td>
                  <q-td key="qty" :props="props" class="text-right">
                    {{ fmtQty(order.qty, order.unit) }}
                  </q-td>
                  <q-td key="norm_per_unit" :props="props" class="text-right">
                    {{ fmt(order.norm_hours_per_unit != null ? order.norm_hours_per_unit : ((Number(order.norm_hours_total || 0)) / (Number(order.qty || 1)))) }}
                  </q-td>
                  <q-td key="norm_total" :props="props" class="text-right">
                    {{ fmt(order.norm_hours_total) }}
                  </q-td>
                </q-tr>
              </template>
            </q-table>
          </template>

          <!-- Группированный вывод по видам производства (если день не выбран) -->
          <template v-else-if="groupedProdRows.length">
            <q-table
              dense
              table-class="compact-rows"
              :rows="groupedProdRows"
              :columns="prodUnifiedColumns"
              row-key="area_id"
              :loading="prod.loading"
              :pagination="{ rowsPerPage: 50 }"
              hide-header
            >
              <template v-slot:body="props">
                <!-- Заголовок группы -->
                <q-tr :props="props" :key="`grp_${props.row.area_id}`">
                  <q-td colspan="100%" class="bg-grey-2">
                    <div class="text-subtitle1">
                      <strong>Вид производства:</strong> {{ props.row.area_name }}
                      <span class="text-grey q-ml-sm">
                        · Заказов: {{ (props.row.orders || []).length }}
                        · Норматив всего: {{ fmt(props.row.norm_sum_hours) }} ч
                      </span>
                      <q-badge v-if="props.row.min_days_to_need != null" class="q-ml-sm" color="orange" outline>
                        Срочн.: {{ props.row.min_days_to_need }} д
                      </q-badge>
                      <q-badge v-if="Number(props.row.cap_overload_hours || 0) > 0" class="q-ml-sm" color="negative" outline>
                        Перегруз: {{ fmt(props.row.cap_overload_hours) }} ч
                      </q-badge>
                    </div>
                  </q-td>
                </q-tr>
                <!-- Строки заказов -->
                <q-tr v-for="order in props.row.orders" :key="order.agg_key || `${order.item_id}|${order.unit || ''}`" :props="props">
                  <q-td key="name" :props="props">
                    <div>{{ order.item_name || ('Номенклатура #' + order.item_id) }}</div>
                  </q-td>
                  <q-td key="article" :props="props">
                    {{ order.item_article || '—' }}
                  </q-td>
                  <q-td key="qty" :props="props" class="text-right">
                    {{ fmtQty(order.qty, order.unit) }}
                  </q-td>
                  <q-td key="norm_per_unit" :props="props" class="text-right">
                    {{ fmt(order.norm_hours_per_unit != null ? order.norm_hours_per_unit : ((Number(order.norm_hours_total || 0)) / (Number(order.qty || 1)))) }}
                  </q-td>
                  <q-td key="norm_total" :props="props" class="text-right">
                    {{ fmt(order.norm_hours_total) }}
                  </q-td>
                </q-tr>
              </template>
            </q-table>
          </template>

          <!-- Фолбэк: плоский список без группировки -->
          <template v-else>
            <q-table
              dense
              table-class="compact-rows"
              :rows="plainProdRows"
              :columns="prodUnifiedColumns"
              row-key="order_id"
              :loading="prod.loading"
              :pagination="{ rowsPerPage: 50 }"
            >
              <template v-slot:body-cell-name="p">
                <q-td :props="p">
                  <div>{{ p.row.item_name || ('Номенклатура #' + p.row.item_id) }}</div>
                </q-td>
              </template>
              <template v-slot:body-cell-qty="p">
                <q-td :props="p" class="text-right">{{ fmtQty(p.row.qty, p.row.unit) }}</q-td>
              </template>
              <template v-slot:body-cell-norm_per_unit="p">
                <q-td :props="p" class="text-right">
                  {{ fmt(p.row.norm_hours_per_unit != null ? p.row.norm_hours_per_unit : ((Number(p.row.norm_hours_total || 0)) / (Number(p.row.qty || 1)))) }}
                </q-td>
              </template>
              <template v-slot:body-cell-norm_total="p">
                <q-td :props="p" class="text-right">
                  {{ fmt(p.row.norm_hours_total) }}
                </q-td>
              </template>
            </q-table>
          </template>
        </q-tab-panel>

        <!-- Purchases unified tab -->
        <q-tab-panel name="purchases">
          <div class="row items-center q-gutter-sm q-mb-sm">
            <div class="text-subtitle2">
              Результаты MRP от {{ summary?.run?.started_at || '—' }}
            </div>
            <q-space />
            <q-select v-model="purch.filter.bucket_type" :options="bucketOptions" emit-value map-options dense outlined label="Бакет" style="width: 150px" />
            <q-input v-model="purch.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
            <q-input v-model="purch.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
            <q-btn dense color="primary" icon="search" @click="loadPurchases()" />
            <q-btn dense flat icon="refresh" @click="loadPurchases()" />
            <q-separator vertical class="q-mx-xs" />
            <q-btn dense flat icon="download" label="CSV" @click="exportPurch('csv')" />
            <q-btn dense flat icon="table_view" label="XLSX" @click="exportPurch('xlsx')" />
          </div>

          <q-table
            dense
            table-class="compact-rows"
            :rows="purchAggRows"
            :columns="purchUnifiedColumns"
            row-key="agg_key"
            :loading="purch.loading"
            :pagination="{ rowsPerPage: 50 }"
          >
            <template v-slot:body-cell-name="p">
              <q-td :props="p">
                <div>{{ p.row.item_name || ('Номенклатура #' + p.row.item_id) }}</div>
              </q-td>
            </template>
            <template v-slot:body-cell-qty="p">
              <q-td :props="p" class="text-right">{{ fmtQty(p.row.qty, p.row.unit) }}</q-td>
            </template>
          </q-table>
        </q-tab-panel>
      </q-tab-panels>
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
          <q-select v-model="prod.filter.bucket_type" :options="bucketOptions" emit-value map-options dense outlined label="Бакет" style="width: 150px" />
          <q-input v-model="prod.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
          <q-input v-model="prod.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
          <q-btn dense color="primary" icon="search" @click="loadProduction()" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadProduction()" />
        </div>
        <q-table
          dense
          table-class="compact-rows"
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
          <q-select v-model="purch.filter.bucket_type" :options="bucketOptions" emit-value map-options dense outlined label="Бакет" style="width: 150px" />
          <q-input v-model="purch.filter.date_from" dense outlined label="От даты (YYYY-MM-DD)" style="width: 200px" />
          <q-input v-model="purch.filter.date_to" dense outlined label="До даты (YYYY-MM-DD)" style="width: 200px" />
          <q-btn dense color="primary" icon="search" @click="loadPurchases()" />
          <q-space />
          <q-btn dense flat icon="refresh" @click="loadPurchases()" />
        </div>
        <q-table
          dense
          table-class="compact-rows"
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
          <q-select v-model="cap.filter.bucket_type" :options="bucketOptions" emit-value map-options dense outlined label="Бакет" style="width: 150px" />
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
  listResources,
  exportPlanningResultProduction,
  exportPlanningResultPurchases
} from '../services/api'
import type { QTableColumn } from 'quasar'
import type { SpecNode } from '../services/api'
const prodColumns: QTableColumn<any>[] = [
  { name: 'order_id', label: 'Order', field: 'order_id', align: 'left', sortable: true },
  { name: 'item_id', label: 'Item', field: 'item_id', align: 'right', sortable: true },
  { name: 'qty', label: 'Qty', field: 'qty', align: 'right', sortable: true },
  { name: 'norm_hours_per_unit', label: 'Норма, ч/шт', field: 'norm_hours_per_unit', align: 'right', sortable: true },
  { name: 'norm_hours_total', label: 'Норматив всего, ч', field: 'norm_hours_total', align: 'right', sortable: true },
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

// Унифицированные колонки для вкладок «Производство» и «Закупки»
const prodUnifiedColumns: QTableColumn<any>[] = [
  { name: 'name', label: 'Наименование', field: 'item_name', align: 'left' },
  { name: 'article', label: 'Артикул', field: 'item_article', align: 'left' },
  { name: 'qty', label: 'Количество', field: 'qty', align: 'right' },
  { name: 'norm_per_unit', label: 'Норма, ч/шт', field: 'norm_hours_per_unit', align: 'right' },
  { name: 'norm_total', label: 'Норматив всего, ч', field: 'norm_hours_total', align: 'right' }
]

const purchUnifiedColumns: QTableColumn<any>[] = [
  { name: 'name', label: 'Наименование', field: 'item_name', align: 'left' },
  { name: 'article', label: 'Артикул', field: 'item_article', align: 'left' },
  { name: 'qty', label: 'Количество', field: 'qty', align: 'right' }
]

const route = useRoute()
const runId = Number(route.params.runId)

const summary = ref<any | null>(null)
const tab = ref<'production' | 'purchases' | 'capacity' | 'pegging' | 'components'>('production')
// Вкладки верхнего уровня для унифицированных таблиц
const viewTab = ref<'production' | 'purchases'>('production')

// Popup флаг для выбора даты «День задания»
const showDayPopup = ref(false)
// Меню выбора даты (QMenu)
const showDayMenu = ref(false)

 // --- Справочники ---
 const itemMap = ref<{ [key: number]: any }>({})
 const areaMap = ref<{ [key: number]: string }>({})
 
 // --- Группировка для новых таблиц ---
 const groupedProductionOrders = ref<any[]>([])
 // Полные наборы строк для верхних таблиц (без учёта пагинации детальных)
 const prodAllRows = ref<any[]>([])
 const purchAllRows = ref<any[]>([])
// Итоговый источник строк для карточки «Рекомендуемые заказы на производство»
const groupedProdRows = computed(() => {
  const groups = groupedProductionOrders.value || []
  if (groups.length > 0) return groups
  // Фолбэк: если группировка по участкам пустая — показываем плоский список,
  // но применяем клиентский фильтр по датам/бакету для консистентности.
  const src = (prodAllRows.value || []).filter(inProdRange)
  const orders = src.map((r: any) => ({
    ...r,
    item_name: (itemMap.value?.[r.item_id]?.item_name) ?? `Номенклатура #${r.item_id}`,
    item_article: (itemMap.value?.[r.item_id]?.item_article) ?? ''
  }))
  return orders.length ? [{ area_id: 0, area_name: '—', orders }] : []
})
// Плоский список для фолбэка
const plainProdRows = computed(() => {
  const src = prodAllRows.value || []
  return src.map((r: any) => ({
    ...r,
    item_name: (itemMap.value?.[r.item_id]?.item_name) ?? `Номенклатура #${r.item_id}`,
    item_article: (itemMap.value?.[r.item_id]?.item_article) ?? ''
  }))
})

// Агрегация закупок по item_id+unit для верхней вкладки (независимо от пагинации детальных)
const purchAggRows = computed(() => {
  const map = new Map<string, any>()
  // Apply UI date filter on full dataset (by bucket_date/order_date/need_date)
  const src = (purchAllRows.value || []).filter(inPurchRange)
  for (const r of src) {
    const key = `${r.item_id}|${r.unit || ''}`
    if (!map.has(key)) {
      map.set(key, {
        agg_key: key,
        item_id: r.item_id,
        item_name: r.item_name || (itemMap.value?.[r.item_id]?.item_name) || `Номенклатура #${r.item_id}`,
        item_article: r.item_article || (itemMap.value?.[r.item_id]?.item_article) || '',
        unit: r.unit,
        qty: 0
      })
    }
    const ex = map.get(key)
    ex.qty = Number(ex.qty || 0) + Number(r.qty || 0)
  }
  return Array.from(map.values())
})

// --- Ежедневная повестка по участкам (задание на день) ---
const dailyAgendaGroups = ref<any[]>([])

function rebuildDailyAgendaForDay() {
  try {
    const day = (prod.filter.day_date || '').slice(0, 10)
    if (!day) {
      dailyAgendaGroups.value = []
      return
    }
    type Agg = {
      item_id: number
      unit?: string | null
      qty: number
      norm_hours_total: number
      norm_hours_per_unit: number | null
      item_name: string
      item_article: string
      agg_key: string
      _added_full_qty?: boolean
    }
    type Group = {
      area_id: number
      area_name: string
      _agg: Record<string, Agg>
    }
    const groups: Record<number, Group> = {}

    const rows = prodAllRows.value || []
    for (const order of rows) {
      const stages: any[] = Array.isArray(order?.stages) ? (order.stages as any[]) : []
      // Норма на штуку из заказа (если нет — вычисляем от общей нормы и qty)
      const q = Number(order?.qty || 0)
      let npu: number | null = null
      if (order?.norm_hours_per_unit != null) {
        npu = Number(order.norm_hours_per_unit)
      } else if (q > 0) {
        npu = Number(order?.norm_hours_total || 0) / q
      }
      for (const s of stages) {
        const sDate = (s?.bucket_date || '').slice(0, 10)
        if (sDate !== day) continue
        const areaId = s?.area_id != null ? Number(s.area_id) : 0
        if (!groups[areaId]) {
          groups[areaId] = {
            area_id: areaId,
            area_name: areaId ? (areaMap.value[areaId] ?? `Вид производства #${areaId}`) : '—',
            _agg: {}
          }
        }
        const key = `${order.item_id}|${order.unit || ''}`
        if (!groups[areaId]._agg[key]) {
          groups[areaId]._agg[key] = {
            item_id: Number(order.item_id),
            unit: order.unit,
            qty: 0,
            norm_hours_total: 0,
            norm_hours_per_unit: npu,
            item_name: order.item_name || (itemMap.value?.[order.item_id]?.item_name) || `Номенклатура #${order.item_id}`,
            item_article: order.item_article || (itemMap.value?.[order.item_id]?.item_article) || '',
            agg_key: key,
            _added_full_qty: false
          }
        }
        const ex = groups[areaId]._agg[key]
        const hours = Number(s?.hours || 0)
        ex.norm_hours_total += hours
        // Перевод часов дня в выпуск по позиции за день по виду производства
        // Если известна норма на штуку — используем час/норму,
        // иначе (npu <= 0) — один раз прибавляем полный объём заказа в этот день.
        if (npu && npu > 0) {
          ex.qty += hours / npu
        } else {
          if (!ex._added_full_qty) {
            ex.qty += Number(order?.qty || 0)
            ex._added_full_qty = true
          }
        }
      }
    }

    // Преобразование в массив и расчёт итогов/перегруза за день
    const out: any[] = []
    for (const areaIdStr of Object.keys(groups)) {
      const areaId = Number(areaIdStr)
      const g = groups[areaId]
      if (!g) continue
      const orders = Object.values(g._agg || {}) as Agg[]
      const normSum = orders.reduce((s, r) => s + Number(r.norm_hours_total || 0), 0)
      const sumQty = orders.reduce((s, r) => s + Number(r.qty || 0), 0)
      // Индикатор перегруза за день
      const cap = (dayCapUpper.value || {})[areaId] || { overload_hours: 0 }
      out.push({
        area_id: g.area_id,
        area_name: g.area_name,
        orders,
        norm_sum_hours: normSum,
        sum_qty: sumQty,
        cap_overload_hours: Number(cap.overload_hours || 0)
      })
    }
    dailyAgendaGroups.value = out
  } catch (e) {
    console.error('Failed to rebuild daily agenda', e)
    dailyAgendaGroups.value = []
  }
}

function rebuildGroupedProductionOrders() {
  // Берём полный набор заказов. Полагаемся на серверные фильтры bucket_type/date,
  // на клиенте валидируем только диапазон по bucket_date (если задан).
  const all = (prodAllRows.value || [])
  const from = emptyToUndef(prod.filter.date_from)
  const to = emptyToUndef(prod.filter.date_to)
  const src = (!from && !to) ? all : all.filter((order: any) => {
    const dt = (order?.bucket_date || null) as string | null
    return dateInRange(dt, from, to)
  })

  if (!src.length) {
    groupedProductionOrders.value = []
    return
  }

  type GroupType = {
    area_id: number
    area_name: string
    orders: any[]
    norm_sum_hours: number
    _agg: Record<string, any>
    min_days_to_need: number | null
  }

  const groups: Record<number, GroupType> = {}

  for (const order of src) {
    const stages: any[] = Array.isArray(order?.stages) ? (order.stages as any[]) : []
    // Определяем «доминирующий» вид производства по стадии с максимальными часами (без доп. фильтров)
    let dominant: any = null
    for (const s of stages) {
      if (!dominant || Number(s?.hours || 0) > Number(dominant?.hours || 0)) dominant = s
    }
    const areaId = dominant?.area_id != null ? Number(dominant.area_id) : 0
    const areaName = areaId ? (areaMap.value[areaId] ?? `Вид производства #${areaId}`) : '—'

    if (!groups[areaId]) {
      groups[areaId] = {
        area_id: areaId,
        area_name: areaName,
        orders: [],
        norm_sum_hours: 0,
        _agg: {},
        min_days_to_need: null
      }
    }
    const g = groups[areaId]

    // Норматив по заказу берём из ответа бэкенда
    const hoursTotalOrder = Number(order?.norm_hours_total || 0)

    // Агрегация по (item_id, unit) внутри вида производства
    const key = `${order.item_id}|${order.unit || ''}`
    if (!g._agg[key]) {
      g._agg[key] = {
        ...order,
        agg_key: key,
        item_name: order.item_name || `Номенклатура #${order.item_id}`,
        item_article: order.item_article || '',
        qty: 0,
        norm_hours_total: 0,
        norm_hours_per_unit: order.norm_hours_per_unit ?? null
      }
    }
    const ex = g._agg[key]
    ex.qty = Number(ex.qty || 0) + Number(order.qty || 0)
    ex.norm_hours_total = Number(ex.norm_hours_total || 0) + hoursTotalOrder
    const q = Number(ex.qty || 0)
    ex.norm_hours_per_unit = q > 0 ? Number(ex.norm_hours_total || 0) / q : (ex.norm_hours_per_unit ?? null)

    // Срочность (минимум дней до потребности по заказам группы)
    try {
      const today = new Date()
      const todayUTC = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()))
      const needStr = (order?.need_date || order?.bucket_date || null) as string | null
      if (needStr) {
        const nd = new Date(needStr.slice(0, 10))
        const ndUTC = new Date(Date.UTC(nd.getFullYear(), nd.getMonth(), nd.getDate()))
        const diffDays = Math.ceil((ndUTC.getTime() - todayUTC.getTime()) / 86400000)
        if (g.min_days_to_need == null || diffDays < g.min_days_to_need) {
          g.min_days_to_need = diffDays
        }
      }
    } catch {}
  }

  // Финализация групп
  const out: any[] = []
  for (const areaIdStr of Object.keys(groups)) {
    const areaId = Number(areaIdStr)
    const g = groups[areaId]
    if (!g) continue
    const orders = Object.values(g._agg || {}) as any[]
    const normSum = orders.reduce((s: number, r: any) => s + Number(r?.norm_hours_total || 0), 0)

    // Индикаторы capacity из предварительно загруженной карты
    const cap = (capUpper.value || {})[areaId] || { overload_hours: 0, overloaded_buckets: 0 }
    out.push({
      area_id: g.area_id,
      area_name: g.area_name,
      orders,
      norm_sum_hours: normSum,
      min_days_to_need: g.min_days_to_need,
      cap_overload_hours: Number(cap.overload_hours || 0),
      cap_overloaded_buckets: Number(cap.overloaded_buckets || 0)
    })
  }
  groupedProductionOrders.value = out
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

const bucketOptions = [
  { label: 'Любой', value: undefined },
  { label: 'daily', value: 'daily' },
  { label: 'weekly', value: 'weekly' }
]

// Production state
const prod = reactive({
  rows: [] as any[],
  loading: false,
  filter: { bucket_type: undefined as 'daily' | 'weekly' | undefined, day_date: '', date_from: '', date_to: '' },
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
  try {
    const n = Number(v ?? 0)
    if (Number.isNaN(n)) return '0,000'
    return new Intl.NumberFormat('ru-RU', {
      minimumFractionDigits: 3,
      maximumFractionDigits: 3,
      useGrouping: true
    }).format(n)
  } catch {
    return '0,000'
  }
}

// Формат количества с единицей измерения
function fmtQty(qty: any, unit?: string | null) {
  const q = fmt(qty)
  const u = (unit || '').toString().trim()
  return u ? `${q} ${u}` : q
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
    const [resp, full] = await Promise.all([
      getPlanningResultProduction(runId, {
        bucket_type: prod.filter.bucket_type,
        date_from: emptyToUndef(prod.filter.date_from),
        date_to: emptyToUndef(prod.filter.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc',
        limit, offset
      }),
      getPlanningResultProduction(runId, {
        bucket_type: prod.filter.bucket_type,
        date_from: emptyToUndef(prod.filter.date_from),
        date_to: emptyToUndef(prod.filter.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc',
        limit: 100000,
        offset: 0
      })
    ])
    prod.rows = resp.rows || []
    prod.pagination.rowsNumber = resp.total || 0
    prodAllRows.value = (full?.rows || [])
    rebuildOrderOptions()
    // Пересобираем группы по полному набору
    rebuildGroupedProductionOrders()
    // Индикаторы capacity для верхнего агрегата (по текущим фильтрам)
    await loadCapacityUpper()
    // Ежедневная повестка + мощность за конкретный день (если выбран)
    rebuildDailyAgendaForDay()
    await loadCapacityUpperDay()
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
    const [resp, full] = await Promise.all([
      getPlanningResultPurchases(runId, {
        bucket_type: purch.filter.bucket_type,
        date_from: emptyToUndef(purch.filter.date_from),
        date_to: emptyToUndef(purch.filter.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc',
        limit, offset
      }),
      getPlanningResultPurchases(runId, {
        bucket_type: purch.filter.bucket_type,
        date_from: emptyToUndef(purch.filter.date_from),
        date_to: emptyToUndef(purch.filter.date_to),
        sort_by: 'item_name',
        sort_dir: 'asc',
        limit: 100000,
        offset: 0
      })
    ])
    purch.rows = resp.rows || []
    purch.pagination.rowsNumber = resp.total || 0
    purchAllRows.value = (full?.rows || [])
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
    const res = await exportPlanningResultProduction(runId, {
      format: fmt,
      bucket_type: prod.filter.bucket_type,
      date_from: emptyToUndef(prod.filter.date_from),
      date_to: emptyToUndef(prod.filter.date_to),
      sort_by: 'item_name',
      sort_dir: 'asc'
    })
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
      bucket_type: purch.filter.bucket_type,
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

// Карта capacity для верхнего агрегата (перегруз по участкам в выбранном периоде)
const capUpper = ref<{ [areaId: number]: { overload_hours: number; hours_planned: number; hours_available: number; overloaded_buckets: number } }>({})
 
async function loadCapacityUpper() {
  try {
    const resp = await getPlanningResultCapacity(runId, {
      bucket_type: prod.filter.bucket_type,
      date_from: emptyToUndef(prod.filter.date_from),
      date_to: emptyToUndef(prod.filter.date_to),
      limit: 100000,
      offset: 0
    })
    const map: { [k: number]: { overload_hours: number; hours_planned: number; hours_available: number; overloaded_buckets: number } } = {}
    for (const r of (resp.rows || [])) {
      const aid = Number(r.area_id || 0)
      if (!map[aid]) {
        map[aid] = { overload_hours: 0, hours_planned: 0, hours_available: 0, overloaded_buckets: 0 }
      }
      map[aid].overload_hours += Number(r.overload_hours || 0)
      map[aid].hours_planned += Number(r.hours_planned || 0)
      map[aid].hours_available += Number(r.hours_available || 0)
      if (Number(r.overload_hours || 0) > 0) {
        map[aid].overloaded_buckets += 1
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
const dayCapUpper = ref<{ [areaId: number]: { overload_hours: number } }>({})

async function loadCapacityUpperDay() {
  try {
    const day = (prod.filter.day_date || '').slice(0, 10)
    if (!day) {
      dayCapUpper.value = {}
      return
    }
    const resp = await getPlanningResultCapacity(runId, {
      bucket_type: 'daily',
      date_from: day,
      date_to: day,
      limit: 100000,
      offset: 0
    })
    const map: { [k: number]: { overload_hours: number } } = {}
    for (const r of (resp.rows || [])) {
      const aid = Number(r.area_id || 0)
      if (!map[aid]) map[aid] = { overload_hours: 0 }
      map[aid].overload_hours += Number(r.overload_hours || 0)
    }
    dayCapUpper.value = map
  } catch (e) {
    console.error('Failed to load capacity for selected day', e)
  }
}
 
// Установить день как фильтр (bucket=daily) и перезагрузить данные
function applyDayFilter() {
  const day = (prod.filter.day_date || '').trim()
  if (!day) return
  prod.filter.bucket_type = 'daily'
  prod.filter.date_from = day
  prod.filter.date_to = day
  // Перезагрузим данные сервера и пересчеты локальных агрегатов
  loadProduction()
  rebuildDailyAgendaForDay()
  loadCapacityUpperDay()
}

// Открыть попап выбора даты
function openDayPicker(e?: Event) {
  try {
    showDayPopup.value = true
  } catch {}
}

// Обработка выбора даты в календаре
function onDayPicked(val: string) {
  try {
    // val уже в маске YYYY-MM-DD
    prod.filter.day_date = (val || '').slice(0, 10)
    // Применяем фильтр на день
    applyDayFilter()
    // Закрываем попап/меню
    showDayPopup.value = false
    showDayMenu.value = false
  } catch {
    // no-op
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

// --- Helpers: date range filters for upper unified tables ---
function dateInRange(dt: string | null | undefined, from?: string, to?: string): boolean {
  if (!dt) return false
  const d = String(dt).slice(0, 10) // YYYY-MM-DD
  if (from && d < from) return false
  if (to && d > to) return false
  return true
}

function inProdRange(row: any): boolean {
  const from = emptyToUndef(prod.filter.date_from)
  const to = emptyToUndef(prod.filter.date_to)
  // Валидация только по bucket_date, без повторной фильтрации по bucket_type (сервер уже отобрал)
  const dt = (row?.bucket_date || null) as string | null
  const okDate = (!from && !to) ? true : dateInRange(dt, from, to)
  return okDate
}

function inPurchRange(row: any): boolean {
  const from = emptyToUndef(purch.filter.date_from)
  const to = emptyToUndef(purch.filter.date_to)
  // Валидация только по дате
  const dt = (row?.bucket_date || row?.order_date || row?.need_date || null) as string | null
  const okDate = (!from && !to) ? true : dateInRange(dt, from, to)
  return okDate
}

onMounted(async () => {
  // День по умолчанию — сегодня (для повестки цеха на день)
  try {
    if (!prod.filter.day_date) {
      const t = new Date()
      const d = new Date(Date.UTC(t.getFullYear(), t.getMonth(), t.getDate()))
      prod.filter.day_date = d.toISOString().slice(0, 10)
    }
  } catch {}
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
  rebuildDailyAgendaForDay()
  await loadCapacityUpperDay()
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

// Автозагрузка при переключении верхних вкладок
watch(viewTab, (vt) => {
  if (vt === 'production' && !prod.rows.length) loadProduction()
  if (vt === 'purchases' && !purch.rows.length) loadPurchases()
})

// Наблюдаем за изменениями prod.rows, itemMap и areaMap для обновления groupedProductionOrders
watch([() => prod.rows, () => itemMap.value, () => areaMap.value], () => {
  rebuildGroupedProductionOrders()
}, { deep: true })

// Пересчёт верхней группировки по изменениям полного набора, фильтров даты и типа бакета
watch([() => prodAllRows.value, () => prod.filter.date_from, () => prod.filter.date_to, () => prod.filter.bucket_type, () => areaMap.value], () => {
  rebuildGroupedProductionOrders()
  // Также пересчитываем дневную повестку и перезагружаем мощность за день, если день выбран
  rebuildDailyAgendaForDay()
  loadCapacityUpperDay()
})

// Перестраиваем верхний агрегат при изменении карты мощностей (индикаторы перегруза)
watch(() => capUpper.value, () => {
  rebuildGroupedProductionOrders()
})

// Ежедневная повестка: пересчёт при изменении выбранного дня/полного набора + принудительный серверный фильтр на день
watch([() => prodAllRows.value, () => prod.filter.day_date], () => {
  const day = (prod.filter.day_date || '').slice(0, 10)
  if (day) {
    // Узкий серверный фильтр (bucket=daily) — предотвращает «сваливание» всего диапазона
    prod.filter.bucket_type = 'daily'
    prod.filter.date_from = day
    prod.filter.date_to = day
    loadProduction()
  }
  rebuildDailyAgendaForDay()
  loadCapacityUpperDay()
})

// Перестраиваем верхний агрегат при изменении карты мощностей (индикаторы перегруза)
watch(() => capUpper.value, () => {
  rebuildGroupedProductionOrders()
})

// Актуализируем индикаторы перегруза при изменении фильтров верхней вкладки «Производство»
watch([() => prod.filter.bucket_type, () => prod.filter.date_from, () => prod.filter.date_to], () => {
  loadCapacityUpper()
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