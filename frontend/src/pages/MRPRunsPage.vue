<template>
  <q-page padding>
    <div class="row items-center q-gutter-sm q-mb-md">
      <div class="text-h5">MRP планирование — прогоны</div>
      <q-space />
      <q-select v-model="form.horizon_days" :options="horizonOptions" dense outlined label="Горизонт (дн.)" style="width: 160px" />
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
      @row-click="onRowClick"
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
          <q-tabs v-model="configTab" dense class="text-grey" active-color="primary" indicator-color="primary" align="left" narrow-indicator>
            <q-tab name="form" label="Форма" />
            <q-tab name="json" label="JSON (расширенный)" />
          </q-tabs>

          <q-tab-panels v-model="configTab" animated>
            <!-- Форма на основе схемы -->
            <q-tab-panel name="form" class="q-pa-none">
              <div class="q-pa-md">
                <q-list separator>
                  <!-- Общие параметры -->
                  <q-item-label header>Общие параметры</q-item-label>
                  <q-item>
                    <q-input v-model.number="configForm.planning_horizon_days" type="number" outlined label="Горизонт планирования (дни)" :min="1" :max="365" class="col-6 q-pr-xs" />
                    <q-input v-model.number="configForm.mps_daily_horizon_days" type="number" outlined label="Горизонт MPS (дни)" :min="1" :max="365" class="col-6 q-pl-xs" />
                  </q-item>
                  <q-item>
                    <q-input v-model.number="configForm.safety_stock_percent" type="number" outlined label="Страховой запас (%)" :min="0" :max="100" :step="0.1" class="col-6 q-pr-xs" />
                  </q-item>

                  <!-- Закупки -->
                  <q-item-label header>Параметры закупок</q-item-label>
                  <q-item>
                    <q-input v-model.number="configForm.procurement.default_lead_time_days" type="number" outlined label="Время поставки по умолчанию (дни)" :min="0" :max="365" class="col-6 q-pr-xs" />
                    <q-select v-model="configForm.procurement.lead_time_min_policy" :options="['max(default_lead_time_days, lead_time_from_item)', 'default_only', 'item_only']" outlined label="Политика расчёта времени поставки" class="col-6 q-pl-xs" />
                  </q-item>
                  <q-item-label header class="q-pl-sm">Партионность</q-item-label>
                  <q-item>
                    <q-select v-model="configForm.procurement.lot_sizing.moq_source" :options="['item_card_or_1', 'item_card_only', 'one_always']" outlined label="Источник MOQ" class="col-4 q-pr-xs" />
                    <q-input v-model.number="configForm.procurement.lot_sizing.multiple" type="number" outlined label="Кратность заказа" :min="1" class="col-4 q-pr-xs q-pl-xs" />
                    <q-select v-model="configForm.procurement.lot_sizing.rounding" :options="['ceil', 'floor', 'round']" outlined label="Метод округления" class="col-4 q-pl-xs" />
                  </q-item>
                  <q-item>
                    <q-select v-model="configForm.procurement.order_date_rounding_policy" :options="['previous_workday', 'current_day', 'next_workday']" outlined label="Округление даты заказа" class="col-6 q-pr-xs" />
                  </q-item>

                  <!-- Производство -->
                  <q-item-label header>Параметры производства</q-item-label>
                  <q-item-label header class="q-pl-sm">Партионность производства</q-item-label>
                  <q-item>
                    <q-input v-model.number="configForm.production.lot_sizing.min_batch" type="number" outlined label="Минимальная партия" :min="1" class="col-4 q-pr-xs" />
                    <q-input v-model.number="configForm.production.lot_sizing.multiple" type="number" outlined label="Кратность партии" :min="1" class="col-4 q-pr-xs q-pl-xs" />
                    <q-select v-model="configForm.production.lot_sizing.rounding" :options="['ceil', 'floor', 'round']" outlined label="Метод округления" class="col-4 q-pl-xs" />
                  </q-item>

                  <!-- Мощности -->
                  <q-item-label header>Параметры мощностей</q-item-label>
                  <q-item>
                    <q-toggle v-model="configForm.capacity.use_resource_calendars" label="Использовать календари ресурсов" class="col-6 q-pr-xs" />
                    <q-toggle v-model="configForm.capacity.consider_power_coefficients" label="Учитывать коэффициенты мощности" class="col-6 q-pl-xs" />
                  </q-item>

                  <!-- Приоритизация -->
                  <q-item-label header>Параметры приоритизации</q-item-label>
                  <q-item>
                    <q-input v-model.number="configForm.prioritization.weight_criticality" type="number" outlined label="Вес критичности" :min="0" :max="1" :step="0.05" class="col-4 q-pr-xs" />
                    <q-input v-model.number="configForm.prioritization.weight_importance" type="number" outlined label="Вес важности" :min="0" :max="1" :step="0.05" class="col-4 q-pr-xs q-pl-xs" />
                    <q-input v-model.number="configForm.prioritization.weight_cycle_time" type="number" outlined label="Вес времени цикла" :min="0" :max="1" :step="0.05" class="col-4 q-pl-xs" />
                  </q-item>
                  <q-item>
                    <q-input v-model.number="configForm.prioritization.default_importance" type="number" outlined label="Важность по умолчанию" :min="0" :max="10" class="col-6 q-pr-xs" />
                  </q-item>

                  <!-- Переключатели -->
                  <q-item-label header>Переключатели</q-item-label>
                  <q-item>
                    <q-toggle v-model="configForm.toggles.include_wip" label="Включать НЗП" class="col-6 q-pr-xs" />
                  </q-item>
                </q-list>
                <div v-if="configFormErrors.length > 0" class="q-mt-md text-red">
                  <div v-for="(error, index) in configFormErrors" :key="index">{{ error }}</div>
                </div>
              </div>
            </q-tab-panel>

            <!-- JSON вкладка -->
            <q-tab-panel name="json" class="q-pa-none">
              <q-card-section class="q-pa-md">
                <q-input v-model="cfgCreate.json" type="textarea" outlined autogrow :input-style="{ fontFamily: 'monospace' }"
                         label="JSON конфигурации (минимум — {} для копии активной)" />
              </q-card-section>
            </q-tab-panel>
          </q-tab-panels>

          <div class="row q-col-gutter-md q-mt-md">
            <div class="col-12 col-md-8" v-if="configTab === 'form'">
              <q-btn color="primary" label="Создать из формы" :loading="cfgCreateLoading" @click="createCfgFromForm" class="q-mr-sm" />
              <q-btn color="secondary" label="Загрузить в форму активную" @click="loadActiveConfigToForm" />
            </div>
            <div class="col-12 col-md-8" v-else>
              <q-btn color="primary" label="Создать из JSON" :loading="cfgCreateLoading" @click="createCfg" />
            </div>
            <div class="col-12 col-md-4">
              <q-input v-model="cfgCreate.comment" outlined label="Комментарий" class="q-mb-sm" />
              <q-input v-model="cfgCreate.created_by" outlined label="Автор" class="q-mb-sm" />
              <q-toggle v-model="cfgCreate.activate" label="Сразу активировать" />
            </div>
          </div>
        </q-card-section>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
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

// --- Schema-driven form ---
// NOTE: В продакшене загружать из public/ или через import в build-time
const schema = {
 "title": "Схема конфигурации планирования",
  "description": "Описание полей конфигурации MRP-планирования для генерации формы в интерфейсе",
  "type": "object",
  "properties": {
    "planning_horizon_days": {
      "type": "number",
      "minimum": 1,
      "maximum": 365,
      "default": 90,
      "title": "Горизонт планирования (дни)",
      "description": "Количество дней вперёд, на которое рассчитывается план"
    },
    "mps_daily_horizon_days": {
      "type": "number",
      "minimum": 1,
      "maximum": 365,
      "default": 90,
      "title": "Горизонт MPS (дни)",
      "description": "Количество дней вперёд для детального планирования MPS"
    },
    "procurement": {
      "type": "object",
      "title": "Параметры закупок",
      "properties": {
        "default_lead_time_days": {
          "type": "number",
          "minimum": 0,
          "maximum": 365,
          "default": 30,
          "title": "Время поставки по умолчанию (дни)",
          "description": "Время поставки в днях, если не задано в карточке номенклатуры"
        },
        "lead_time_min_policy": {
          "type": "string",
          "enum": ["max(default_lead_time_days, lead_time_from_item)", "default_only", "item_only"],
          "default": "max(default_lead_time_days, lead_time_from_item)",
          "title": "Политика расчёта времени поставки",
          "description": "Правило выбора времени поставки при наличии значения в карточке номенклатуры"
        },
        "lot_sizing": {
          "type": "object",
          "title": "Партионность",
          "properties": {
            "moq_source": {
              "type": "string",
              "enum": ["item_card_or_1", "item_card_only", "one_always"],
              "default": "item_card_or_1",
              "title": "Источник MOQ",
              "description": "Откуда брать минимальный заказ (MOQ): из карточки номенклатуры или 1"
            },
            "multiple": {
              "type": "number",
              "minimum": 1,
              "default": 1,
              "title": "Кратность заказа",
              "description": "Заказы будут округляться до этого числа"
            },
            "rounding": {
              "type": "string",
              "enum": ["ceil", "floor", "round"],
              "default": "ceil",
              "title": "Метод округления",
              "description": "Как округлять количество при расчёте заказа"
            }
          }
        },
        "order_date_rounding_policy": {
          "type": "string",
          "enum": ["previous_workday", "current_day", "next_workday"],
          "default": "previous_workday",
          "title": "Округление даты заказа",
          "description": "Как округлять дату заказа по календарю"
        }
      }
    },
    "production": {
      "type": "object",
      "title": "Параметры производства",
      "properties": {
        "lot_sizing": {
          "type": "object",
          "title": "Партионность производства",
          "properties": {
            "min_batch": {
              "type": "number",
              "minimum": 1,
              "default": 1,
              "title": "Минимальная партия",
              "description": "Минимальное количество в производственном заказе"
            },
            "multiple": {
              "type": "number",
              "minimum": 1,
              "default": 1,
              "title": "Кратность партии",
              "description": "Производственные заказы будут кратны этому числу"
            },
            "rounding": {
              "type": "string",
              "enum": ["ceil", "floor", "round"],
              "default": "ceil",
              "title": "Метод округления",
              "description": "Как округлять количество при расчёте заказа"
            }
          }
        }
      }
    },
    "safety_stock_percent": {
      "type": "number",
      "minimum": 0,
      "maximum": 100,
      "default": 1,
      "multipleOf": 0.1,
      "title": "Страховой запас (%)",
      "description": "Процент от спроса для формирования страхового запаса"
    },
    "capacity": {
      "type": "object",
      "title": "Параметры мощностей",
      "properties": {
        "use_resource_calendars": {
          "type": "boolean",
          "default": true,
          "title": "Использовать календари ресурсов",
          "description": "Учитывать календари недоступности ресурсов при расчёте"
        },
        "consider_power_coefficients": {
          "type": "boolean",
          "default": true,
          "title": "Учитывать коэффициенты мощности",
          "description": "Применять коэффициенты эффективности ресурсов"
        }
      }
    },
    "prioritization": {
      "type": "object",
      "title": "Параметры приоритизации",
      "properties": {
        "weight_criticality": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "default": 0.4,
          "multipleOf": 0.05,
          "title": "Вес критичности",
          "description": "Вклад критичности в общий приоритет (сумма = 1.0)"
        },
        "weight_importance": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "default": 0.3,
          "multipleOf": 0.05,
          "title": "Вес важности",
          "description": "Вклад важности в общий приоритет (сумма = 1.0)"
        },
        "weight_cycle_time": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "default": 0.3,
          "multipleOf": 0.05,
          "title": "Вес времени цикла",
          "description": "Вклад времени цикла в общий приоритет (сумма = 1.0)"
        },
        "default_importance": {
          "type": "number",
          "minimum": 0,
          "maximum": 10,
          "default": 1,
          "title": "Важность по умолчанию",
          "description": "Значение важности для номенклатуры без явного указания"
        }
      }
    },
    "toggles": {
      "type": "object",
      "title": "Переключатели",
      "properties": {
        "include_wip": {
          "type": "boolean",
          "default": false,
          "title": "Включать НЗП",
          "description": "Учитывать незавершённое производство в расчётах"
        },
      }
    }
  },
  "required": ["planning_horizon_days", "mps_daily_horizon_days", "safety_stock_percent"],
  "additionalProperties": false
};

// --- Вспомогательные функции для работы с JSON Schema ---
type SchemaField = {
  type: 'string' | 'number' | 'boolean' | 'object';
  title: string;
  description?: string;
  enum?: string[];
  minimum?: number;
  maximum?: number;
  multipleOf?: number;
  default?: any;
  properties?: { [key: string]: SchemaField };
};

type SchemaDef = {
  properties: { [key: string]: SchemaField };
  required: string[];
};

type ConfigForm = {
  planning_horizon_days: number;
  mps_daily_horizon_days: number;
  procurement: {
    default_lead_time_days: number;
    lead_time_min_policy: string;
    lot_sizing: {
      moq_source: string;
      multiple: number;
      rounding: string;
    };
    order_date_rounding_policy: string;
  };
  production: {
    lot_sizing: {
      min_batch: number;
      multiple: number;
      rounding: string;
    };
  };
  safety_stock_percent: number;
  capacity: {
    use_resource_calendars: boolean;
    consider_power_coefficients: boolean;
  };
  prioritization: {
    weight_criticality: number;
    weight_importance: number;
    weight_cycle_time: number;
    default_importance: number;
  };
  toggles: {
    include_wip: boolean;
  };
};

// --- Генерация формы по схеме ---
function createDefaultConfig(): ConfigForm {
  const result: any = {};
 const props = schema.properties as { [key: string]: SchemaField };
 for (const key in props) {
   const field = (props as any)[key] as SchemaField | undefined;
   if (!field) continue;
   if (field.type === 'object' && field.properties) {
     result[key] = createDefaultObject(field.properties, field.default || {});
   } else {
     result[key] = field?.default;
   }
 }
  return result;
}

function createDefaultObject(props: { [key: string]: SchemaField }, base: any = {}): any {
  const result: any = { ...base };
  for (const key in props) {
    const field = (props as any)[key] as SchemaField | undefined;
    if (!field) continue;
    if (field.type === 'object' && field.properties) {
      result[key] = createDefaultObject(field.properties, base[key] || {});
    } else {
      result[key] = field?.default;
    }
  }
  return result;
}

// --- Валидация формы ---
function validateConfig(config: ConfigForm): { valid: boolean; errors: string[] } {
  const errors: string[] = [];
  // Валидация весов приоритизации
 const sum = (config.prioritization.weight_criticality || 0) + 
              (config.prioritization.weight_importance || 0) + 
              (config.prioritization.weight_cycle_time || 0);
  if (Math.abs(sum - 1.0) > 1e-6) {
    errors.push('Сумма весов приоритизации должна быть равна 1.0');
  }
 return { valid: errors.length === 0, errors };
}

// --- Состояние формы ---
const configForm = ref<ConfigForm>(createDefaultConfig());
const configFormErrors = ref<string[]>([]);
const configTab = ref<'form' | 'json'>('form');

// --- Валидация и сохранение формы ---
async function createCfgFromForm() {
  const validation = validateConfig(configForm.value);
  if (!validation.valid) {
    configFormErrors.value = validation.errors;
    $q.notify({ type: 'negative', message: 'Ошибка валидации формы: ' + validation.errors.join('; ') });
    return;
  }

  cfgCreateLoading.value = true;
  try {
    const body = {
      config: configForm.value,
      comment: cfgCreate.comment || undefined,
      created_by: cfgCreate.created_by || undefined,
      activate: !!cfgCreate.activate
    };
    const resp = await createPlanningConfig(body);
    $q.notify({ type: 'positive', message: `Создана версия #${resp?.created?.id ?? ''}` });
    // Сброс формы и обновление списков
    configForm.value = createDefaultConfig();
    cfgCreate.comment = '';
    await Promise.all([loadConfigs(), loadActiveConfig()]);
  } catch (e) {
    console.error('Failed to create config from form', e);
    $q.notify({ type: 'negative', message: 'Ошибка создания конфигурации' });
  } finally {
    cfgCreateLoading.value = false;
  }
}

// --- Загрузка активной конфигурации в форму ---
async function loadActiveConfigToForm() {
  try {
    const resp = await getActivePlanningConfig();
    const config = resp?.config || resp || {};
    // Загружаем поля в форму, заполняя недостающие значения по умолчанию
    configForm.value = createDefaultObject(schema.properties as { [key: string]: SchemaField }, config);
  } catch (e) {
    console.error('Failed to load active config to form', e);
  }
}

// --- Функции для совместимости с остальной частью компонента ---
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
  horizon_days: 90 as number
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

function onRowClick(props: any) {
  try {
    const runId = Number(props?.row?.run_id || 0)
    if (Number.isFinite(runId) && runId > 0) {
      goRun(runId)
    }
  } catch (e) {
    console.error('onRowClick failed', e)
  }
}

async function onCalc() {
  calcLoading.value = true
  try {
    const res = await startPlanningRun({
      horizon_days: form.horizon_days,
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