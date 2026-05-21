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
      <q-btn
        color="positive"
        icon="factory"
        label="Произвести"
        :disable="produceCandidate === null"
        :loading="produceLoading"
        @click="openProduceDialog"
      >
        <q-tooltip v-if="selected.length !== 1">
          Выберите ровно одну строку с положительным остатком к выпуску
        </q-tooltip>
        <q-tooltip v-else-if="produceCandidate === null">
          У выбранной строки нечего производить (remaining_qty = 0)
        </q-tooltip>
      </q-btn>
      <q-btn
        color="warning"
        icon="undo"
        label="Вернуть остаток"
        :disable="returnCandidate === null"
        :loading="returnLoading"
        @click="openReturnDialog"
      >
        <q-tooltip v-if="selected.length !== 1">
          Выберите ровно одну частично произведённую строку
        </q-tooltip>
        <q-tooltip v-else-if="returnCandidate === null">
          Доступно только для строк со статусом «Произведён частично»
        </q-tooltip>
      </q-btn>
      <q-btn
        color="accent"
        icon="cloud_upload"
        label="Выгрузить заказ в 1С"
        :disable="exportableOrderIds.length === 0"
        :loading="exportLoading"
        @click="openExportDialog"
      >
        <q-tooltip v-if="selected.length > 0 && exportableOrderIds.length === 0">
          Выбранные строки уже выгружены или принадлежат заказам из 1С
        </q-tooltip>
      </q-btn>
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

    <!-- Produce dialog: bumps produced_qty + optionally exports to 1C -->
    <q-dialog v-model="produceDialog" persistent>
      <q-card style="min-width: 540px; max-width: 95vw;">
        <q-card-section class="row items-center q-pb-none">
          <div class="text-h6">Произвести</div>
          <q-space />
          <q-btn flat round dense icon="close" v-close-popup />
        </q-card-section>

        <q-card-section v-if="produceCandidate">
          <div class="text-body2 q-mb-sm">
            Деталь: <b>{{ produceCandidate.item_name }}</b>
            <span class="text-grey-7">({{ produceCandidate.item_article || '—' }})</span>
          </div>
          <div class="text-caption text-grey-7 q-mb-md">
            Заказ № {{ produceCandidate.order_number }} ·
            строка {{ produceCandidate.line_number || produceCandidate.product_id }} ·
            остаток к выпуску: <b>{{ produceCandidate.remaining_qty }}</b> {{ produceCandidate.unit || '' }}
          </div>

          <div class="row q-col-gutter-md">
            <div class="col-12 col-md-4">
              <q-input
                v-model.number="produceForm.qty"
                type="number"
                dense
                outlined
                label="Количество"
                :rules="produceQtyRules"
                :min="0"
                :max="produceCandidate.remaining_qty"
              />
            </div>
            <div class="col-12 col-md-8">
              <q-input v-model="produceForm.executor" dense outlined label="Исполнитель" />
            </div>
            <div class="col-12">
              <q-input v-model="produceForm.comment" dense outlined label="Комментарий" type="textarea" autogrow />
            </div>
          </div>

          <q-separator class="q-my-sm" />

          <q-checkbox
            v-model="produceForm.export_to_1c"
            label="Сразу выгрузить выпуск в 1С (Document_СборкаЗапасов)"
            color="accent"
          />
          <q-checkbox
            v-if="produceForm.export_to_1c"
            v-model="produceForm.dry_run"
            label="Только dry-run (показать payload, не писать)"
            color="primary"
            class="block"
          />
          <q-banner
            v-if="produceForm.export_to_1c && !produceForm.dry_run"
            dense
            class="bg-blue-1 text-primary q-mt-sm"
          >
            <template #avatar><q-icon name="info" /></template>
            Запись только в demo-базу 1С (URL должен содержать <code>unf_demo</code>).
          </q-banner>

          <div v-if="produceResult" class="q-mt-md">
            <div class="text-subtitle2">Результат:</div>
            <div class="text-caption">
              Выпущено: <b>{{ produceResult.qty }}</b>, всего по строке:
              <b>{{ produceResult.produced_qty_total }}</b>, остаток:
              <b>{{ produceResult.remaining_qty }}</b>. Статус линии:
              <q-chip dense :color="entryColor(produceResult.line_status)" text-color="white">
                {{ produceResult.line_status }}
              </q-chip>
            </div>
            <div v-if="produceExportResult" class="q-mt-sm">
              <q-badge v-if="produceExportResult.dry_run" color="warning" outline>DRY-RUN</q-badge>
              <q-badge v-else-if="produceExportResult.manufactures_created > 0" color="positive" outline>
                В 1С создано: {{ produceExportResult.manufactures_created }}
              </q-badge>
              <q-badge v-if="produceExportResult.manufactures_error > 0" color="negative" outline>
                Ошибок 1С: {{ produceExportResult.manufactures_error }}
              </q-badge>
              <div v-if="produceExportResult.entries?.[0]?.target_ref_key" class="text-caption text-grey-7 q-mt-xs">
                Ref_Key: {{ produceExportResult.entries[0].target_ref_key }}
              </div>
            </div>
          </div>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat label="Закрыть" v-close-popup />
          <q-btn
            color="positive"
            :label="produceForm.export_to_1c
              ? (produceForm.dry_run ? 'Произвести + dry-run' : 'Произвести + выгрузить')
              : 'Произвести'"
            :loading="produceLoading"
            :disable="!produceForm.qty || produceForm.qty <= 0"
            @click="runProduce"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Return-leftover dialog -->
    <q-dialog v-model="returnDialog" persistent>
      <q-card style="min-width: 560px; max-width: 95vw;">
        <q-card-section class="row items-center q-pb-none">
          <div class="text-h6">Вернуть остаток компонентов</div>
          <q-space />
          <q-btn flat round dense icon="close" v-close-popup />
        </q-card-section>

        <q-card-section v-if="returnCandidate">
          <div class="text-body2 q-mb-sm">
            Деталь: <b>{{ returnCandidate.item_name }}</b>
            <span class="text-grey-7">({{ returnCandidate.item_article || '—' }})</span>
          </div>
          <div class="text-caption text-grey-7 q-mb-md">
            Заказ № {{ returnCandidate.order_number }} ·
            строка {{ returnCandidate.line_number || returnCandidate.product_id }} ·
            произведено: <b>{{ returnCandidate.produced_qty }}</b>,
            остаток к выпуску: <b>{{ returnCandidate.remaining_qty }}</b>
          </div>
          <div class="text-caption text-grey-8 q-mb-sm">
            Будет создан черновик <b>Document_ПеремещениеЗапасов</b>
            (направление workshop → исходный склад) с остатком компонентов,
            не использованных при частичном выпуске. В 1С этот документ
            отправляется отдельно через «Создать выдачу» (он попадёт в общий
            экспорт как обычная выдача).
          </div>

          <div v-if="returnResult" class="q-mt-md">
            <div v-if="returnResult.status === 'skipped'">
              <q-banner dense class="bg-orange-1 text-orange-10">
                <template #avatar><q-icon name="info" /></template>
                Не удалось создать возврат: {{ returnResult.skipped_reason }}
              </q-banner>
            </div>
            <div v-else>
              <div class="text-subtitle2 q-mb-xs">
                <q-chip v-if="returnResult.reused" dense color="grey-7" text-color="white">
                  Уже существует черновик
                </q-chip>
                <q-chip v-else dense color="positive" text-color="white">
                  Создан черновик № {{ returnResult.document_number }}
                </q-chip>
              </div>
              <div class="text-caption text-grey-7 q-mb-xs">
                Источник: <code>{{ returnResult.source_warehouse_ref1c || '—' }}</code>
                → получатель: <code>{{ returnResult.destination_warehouse_ref1c || '—' }}</code>
              </div>
              <q-table
                v-if="returnResult.lines.length > 0"
                dense
                flat
                bordered
                :rows="returnResult.lines"
                :columns="returnLineColumns"
                row-key="component_item_id"
                hide-bottom
                :pagination="{ rowsPerPage: 100 }"
              />
            </div>
          </div>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat label="Закрыть" v-close-popup />
          <q-btn
            v-if="!returnResult || returnResult.status !== 'ok'"
            color="warning"
            label="Создать возврат"
            :loading="returnLoading"
            @click="runReturn"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Export to 1C dialog: production orders (Document_ЗаказНаПроизводство) -->
    <q-dialog v-model="exportDialog" persistent>
      <q-card style="min-width: 640px; max-width: 95vw;">
        <q-card-section class="row items-center q-pb-none">
          <div class="text-h6">Выгрузка заказов в 1С</div>
          <q-space />
          <q-btn flat round dense icon="close" v-close-popup />
        </q-card-section>

        <q-card-section>
          <div class="text-body2 q-mb-sm">
            Будет выгружено как <b>Document_ЗаказНаПроизводство</b>, непроведённым
            (Posted=false). Документ остаётся в 1С на ручное проведение администратором.
          </div>
          <div class="text-caption text-grey-7 q-mb-md">
            Уникальных заказов к выгрузке: <b>{{ exportableOrderIds.length }}</b>
            (из {{ selected.length }} выбранных строк журнала).
          </div>

          <q-option-group
            v-model="exportMode"
            :options="exportModeOptions"
            color="primary"
            inline
          />

          <q-banner
            v-if="exportMode === 'apply' && !exportAllowProduction"
            dense
            class="bg-blue-1 text-primary q-mt-sm"
          >
            <template #avatar><q-icon name="info" /></template>
            Запись только в demo-базу 1С (URL должен содержать <code>unf_demo</code>).
          </q-banner>
          <q-banner
            v-else-if="exportMode === 'apply' && exportAllowProduction"
            dense
            class="bg-orange-2 text-negative q-mt-sm"
          >
            <template #avatar><q-icon name="warning" /></template>
            <b>Внимание!</b> Включён режим записи в продовую базу 1С.
            Используйте только если уверены.
          </q-banner>

          <q-checkbox
            v-if="exportMode === 'apply'"
            v-model="exportAllowProduction"
            label="Разрешить запись в non-demo базу (с осторожностью)"
            color="negative"
            class="q-mt-sm"
          />

          <q-separator class="q-my-md" />

          <div v-if="exportResult" class="q-mt-sm">
            <div class="text-subtitle2 q-mb-xs">Результат:</div>
            <div class="q-gutter-xs">
              <q-badge color="primary" outline>Запрошено: {{ exportResult.orders_requested }}</q-badge>
              <q-badge color="info" outline>К выгрузке: {{ exportResult.orders_eligible }}</q-badge>
              <q-badge color="grey-7" outline>Уже выгружены: {{ exportResult.orders_already_linked }}</q-badge>
              <q-badge v-if="!exportResult.dry_run" color="positive" outline>
                Создано: {{ exportResult.orders_created }}
              </q-badge>
              <q-badge v-if="exportResult.orders_error > 0" color="negative" outline>
                Ошибок: {{ exportResult.orders_error }}
              </q-badge>
              <q-badge v-if="exportResult.dry_run" color="warning" outline>DRY-RUN</q-badge>
            </div>

            <q-list
              v-if="(exportResult.entries || []).length > 0"
              dense
              bordered
              separator
              class="q-mt-sm"
              style="max-height: 240px; overflow-y: auto;"
            >
              <q-item v-for="(e, idx) in exportResult.entries" :key="idx">
                <q-item-section>
                  <q-item-label>
                    <q-chip
                      dense
                      :color="entryColor(e.status)"
                      text-color="white"
                      class="q-mr-xs"
                    >
                      {{ e.status }}
                    </q-chip>
                    №{{ e.number || e.document_number || '-' }}
                    <span v-if="e.target_ref_key" class="text-caption text-grey-7">
                      → {{ e.target_ref_key }}
                    </span>
                  </q-item-label>
                  <q-item-label v-if="e.reason || e.error" caption class="text-grey-8">
                    {{ e.error || e.reason }}
                  </q-item-label>
                </q-item-section>
              </q-item>
            </q-list>

            <q-list
              v-if="(exportResult.skipped_rows || []).length > 0"
              dense
              bordered
              separator
              class="q-mt-sm"
            >
              <q-item-label header>Пропущены:</q-item-label>
              <q-item v-for="(s, idx) in exportResult.skipped_rows" :key="`skip-${idx}`">
                <q-item-section>
                  <q-item-label caption>order_id={{ s.order_id }}: {{ s.reason }}</q-item-label>
                </q-item-section>
              </q-item>
            </q-list>
          </div>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat label="Закрыть" v-close-popup />
          <q-btn
            color="primary"
            :label="exportMode === 'dry' ? 'Показать payload' : 'Выгрузить в 1С'"
            :loading="exportLoading"
            :disable="exportableOrderIds.length === 0"
            @click="runExport"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>

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
import { computed, onMounted, reactive, ref } from 'vue'
import { useQuasar } from 'quasar'
import type { QTableColumn } from 'quasar'
import {
  createProductionMaterialIssues,
  deleteProductionControlIgnoredWarehouse,
  deleteProductionControlWorkshopBinding,
  exportManufacturesTo1C,
  exportProductionOrdersTo1C,
  getProductionControlMaterials,
  getProductionControlSettings,
  listResources,
  listProductionControlOrders,
  produceProductionLine,
  returnLeftoverComponents,
  updateProductionControlOrderState,
  upsertProductionControlIgnoredWarehouse,
  upsertProductionControlWorkshopBinding,
  type ExportManufacturesResult,
  type ExportProductionOrdersResult,
  type IgnoredWarehouseEntry,
  type ProduceLineResult,
  type ProductionControlOrderRow,
  type ProductionControlSettings,
  type ReturnLeftoverResult,
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
// "Произвести" dialog: bumps produced_qty + optionally exports to 1C as
// Document_СборкаЗапасов.
// Backends: POST /orders/{id}/produce  +  POST /manufactures/export-to-1c.
// ---------------------------------------------------------------------------
const produceDialog = ref(false)
const produceLoading = ref(false)
const produceForm = reactive<{
  qty: number | null
  executor: string
  comment: string
  export_to_1c: boolean
  dry_run: boolean
}>({
  qty: null,
  executor: '',
  comment: '',
  export_to_1c: false,
  dry_run: true
})
const produceResult = ref<ProduceLineResult | null>(null)
const produceExportResult = ref<ExportManufacturesResult | null>(null)

const produceCandidate = computed<ProductionControlOrderRow | null>(() => {
  // Single-selected row with positive remaining qty.
  if (selected.value.length !== 1) return null
  const row = selected.value[0]
  return (row.remaining_qty || 0) > 0 ? row : null
})

const produceQtyRules = [
  (v: any) => (v != null && Number(v) > 0) || 'Количество должно быть > 0',
  (v: any) => {
    const cand = produceCandidate.value
    if (!cand) return true
    return Number(v) <= Number(cand.remaining_qty || 0) || 'Не больше остатка'
  }
]

function openProduceDialog() {
  const cand = produceCandidate.value
  if (!cand) return
  produceForm.qty = Number(cand.remaining_qty) || null
  produceForm.executor = ''
  produceForm.comment = ''
  produceForm.export_to_1c = false
  produceForm.dry_run = true
  produceResult.value = null
  produceExportResult.value = null
  produceDialog.value = true
}

async function runProduce() {
  const cand = produceCandidate.value
  if (!cand || !produceForm.qty || produceForm.qty <= 0) return
  produceLoading.value = true
  try {
    const r = await produceProductionLine(cand.product_id, {
      qty: Number(produceForm.qty),
      executor: produceForm.executor.trim() || null,
      comment: produceForm.comment.trim() || null
    })
    produceResult.value = r
    $q.notify({
      type: 'positive',
      message: r.line_status === 'produced'
        ? `Готово: ${r.qty}. Строка закрыта.`
        : `Готово: ${r.qty}. Остаток: ${r.remaining_qty}.`
    })

    if (produceForm.export_to_1c) {
      try {
        const exp = await exportManufacturesTo1C({
          manufacture_ids: [r.manufacture_id],
          dry_run: produceForm.dry_run,
          allow_production: false
        })
        produceExportResult.value = exp
        if (!exp.dry_run) {
          const created = exp.manufactures_created || 0
          const errored = exp.manufactures_error || 0
          if (errored === 0) {
            $q.notify({ type: 'positive', message: `В 1С создано: ${created}` })
          } else {
            $q.notify({
              type: 'warning',
              message: `1С: создано ${created}, ошибок ${errored}.`
            })
          }
        }
      } catch (e: any) {
        const detail = e?.response?.data?.detail || e?.message || String(e)
        if (e?.response?.status === 403) {
          $q.notify({
            type: 'negative',
            timeout: 8000,
            message: `Демо-guard: ${detail}. Локальный выпуск всё равно сохранён.`
          })
        } else {
          $q.notify({ type: 'negative', message: `Ошибка экспорта в 1С: ${detail}` })
        }
      }
    }

    await fetchRows()
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || String(e)
    $q.notify({ type: 'negative', message: `Не удалось произвести: ${detail}` })
  } finally {
    produceLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// Return-leftover dialog (workshop -> source for partial production).
// Backend: POST /v1/production-control/orders/{product_id}/return-leftovers.
// ---------------------------------------------------------------------------
const returnDialog = ref(false)
const returnLoading = ref(false)
const returnResult = ref<ReturnLeftoverResult | null>(null)

const returnCandidate = computed<ProductionControlOrderRow | null>(() => {
  if (selected.value.length !== 1) return null
  const row = selected.value[0]
  // Status comes from the "Обеспечение" column; we offer the action only
  // for partially-produced lines.
  if (row.status !== 'produced_partial') return null
  return row
})

const returnLineColumns = [
  { name: 'component_item_id', label: 'item_id', field: 'component_item_id', align: 'left' as const },
  { name: 'issued_qty', label: 'Выдано', field: 'issued_qty', align: 'right' as const },
  { name: 'consumed_qty', label: 'Потреблено', field: 'consumed_qty', align: 'right' as const },
  { name: 'leftover_qty', label: 'К возврату', field: 'leftover_qty', align: 'right' as const },
  { name: 'unit', label: 'ЕИ', field: 'unit', align: 'left' as const },
]

function openReturnDialog() {
  if (returnCandidate.value === null) return
  returnResult.value = null
  returnDialog.value = true
}

async function runReturn() {
  const cand = returnCandidate.value
  if (!cand) return
  returnLoading.value = true
  try {
    const r = await returnLeftoverComponents(cand.product_id)
    returnResult.value = r
    if (r.status === 'ok') {
      if (r.reused) {
        $q.notify({
          type: 'info',
          message: `Уже есть черновик возврата № ${r.document_number}`
        })
      } else {
        $q.notify({
          type: 'positive',
          message: `Создан возврат № ${r.document_number}, позиций: ${r.lines.length}`
        })
      }
      await fetchRows()
    } else {
      $q.notify({
        type: 'warning',
        timeout: 6000,
        message: `Пропущено: ${r.skipped_reason}`
      })
    }
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || String(e)
    $q.notify({ type: 'negative', message: `Ошибка возврата: ${detail}` })
  } finally {
    returnLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// Export-to-1C dialog (production orders -> Document_ЗаказНаПроизводство).
// Backend: POST /v1/production-control/orders/export-to-1c.
// ---------------------------------------------------------------------------
const exportDialog = ref(false)
const exportLoading = ref(false)
const exportMode = ref<'dry' | 'apply'>('dry')
const exportAllowProduction = ref(false)
const exportResult = ref<ExportProductionOrdersResult | null>(null)
const exportModeOptions = [
  { label: 'Dry-run (только показать payload)', value: 'dry' },
  { label: 'Запись в 1С', value: 'apply' }
]

const exportableOrderIds = computed<number[]>(() => {
  // Unique order_ids among selected rows that look like internal MRP orders:
  // source='mrp' AND no order_ref1c stamped yet. Rows from 1C are hidden;
  // already-exported MRP rows still pass through and get reported as
  // 'existing' on the backend (the button itself stays enabled so the user
  // can re-confirm and see the empty result).
  const set = new Set<number>()
  for (const row of selected.value) {
    if (row.order_source !== 'mrp') continue
    set.add(row.order_id)
  }
  return Array.from(set)
})

function entryColor(status: string): string {
  return ({
    created: 'positive',
    existing: 'grey-7',
    error: 'negative',
    skipped: 'orange',
    planned: 'blue'
  } as Record<string, string>)[status] || 'grey'
}

function openExportDialog() {
  exportResult.value = null
  exportMode.value = 'dry'
  exportAllowProduction.value = false
  exportDialog.value = true
}

async function runExport() {
  if (exportableOrderIds.value.length === 0) return
  exportLoading.value = true
  try {
    const result = await exportProductionOrdersTo1C({
      order_ids: exportableOrderIds.value,
      dry_run: exportMode.value === 'dry',
      allow_production: exportAllowProduction.value
    })
    exportResult.value = result
    if (exportMode.value === 'apply') {
      const created = result.orders_created || 0
      const errored = result.orders_error || 0
      if (errored === 0) {
        $q.notify({ type: 'positive', message: `Создано в 1С: ${created}` })
      } else {
        $q.notify({
          type: 'warning',
          message: `Создано: ${created}, ошибок: ${errored}. Подробности в диалоге.`
        })
      }
      // Refresh journal so new order_ref1c is reflected in source/status.
      await fetchRows()
    }
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || String(e)
    if (e?.response?.status === 403) {
      $q.notify({
        type: 'negative',
        timeout: 8000,
        message: `Отказ: ${detail}. Поставьте «Разрешить запись в non-demo базу» если действительно хотите.`
      })
    } else {
      $q.notify({ type: 'negative', message: `Ошибка экспорта: ${detail}` })
    }
  } finally {
    exportLoading.value = false
  }
}

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
