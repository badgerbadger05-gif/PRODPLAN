<template>
  <q-page class="q-pa-lg">
    <div class="row justify-center">
      <div class="col-12">
        <q-card>
          <q-card-section>
            <div class="text-h5">План выпуска техники квартальный</div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <!-- Панель управления -->
            <div class="row items-center gap-2 w-full mb-4 flex-wrap control-bar">
              <q-btn
                color="positive"
                label="Сохранить изменения"
                @click="saveChanges"
                :loading="loading.save"
              />
            </div>

            <!-- Таблица плана (недели текущего квартала) -->
            <div class="table-scroll">
              <q-table
              :rows="rowData"
              :columns="columns"
              :pagination="pagination"
              :loading="loading.table"
              row-key="item_id"
              flat
              class="production-plan-table"
              table-class="wide-table"
              :table-style="{ width: 'max-content', whiteSpace: 'nowrap' }"
              :wrap-cells="false"
              @request="onRequest"
            >
              <!-- Заголовки с переключателем разворота недель -->
              <template v-slot:header-cell="hprops">
                <q-th :props="hprops" :class="hprops.col.headerClasses">
                  <div v-if="hprops.col.name && hprops.col.name.startsWith('week_')" class="row items-center no-wrap justify-between">
                    <span>{{ hprops.col.label }}</span>
                    <q-btn
                      dense
                      flat
                      size="sm"
                      :icon="isWeekExpanded(hprops.col.name.replace('week_', '')) ? 'chevron_left' : 'chevron_right'"
                      @click.stop="toggleWeekExpand(hprops.col.name.replace('week_', ''))"
                    />
                  </div>
                  <div v-else>
                    {{ hprops.col.label }}
                  </div>
                </q-th>
              </template>
              <!-- Кастомные слоты для редактируемых ячеек -->
              <template v-slot:body-cell="props">
                <q-td :props="props">
                  <div v-if="props.col.name === 'actions'">
                    <div class="actions-cell">
                      <q-btn
                        flat
                        round
                        dense
                        size="8px"
                        icon="description"
                        color="primary"
                        class="compact-action-btn"
                        @click="openSpecification(props.row)"
                      >
                        <q-tooltip>Открыть спецификацию</q-tooltip>
                      </q-btn>
                      <q-btn
                        flat
                        round
                        dense
                        size="8px"
                        icon="delete_outline"
                        color="negative"
                        class="compact-action-btn"
                        :loading="deletingId === props.row.item_id"
                        :disable="deletingId === props.row.item_id"
                        @click="onDeleteRow(props.row)"
                      >
                        <q-tooltip>Удалить строку</q-tooltip>
                      </q-btn>
                    </div>
                  </div>
                  <div v-else-if="props.col.name === 'rownum'">
                    <div class="text-right">
                      {{ computeRowNum(props) }}
                    </div>
                  </div>
                  <div v-else-if="props.col.name === 'item_name'" class="item-name-cell">
                    <span class="item-name-full">{{ props.value }}</span>
                    <div class="text-caption text-grey-7">
                      {{ props.row.item_article || '—' }} · {{ props.row.item_code }}
                    </div>
                  </div>
                  <div v-else-if="props.col.name.startsWith('week_')" class="row items-center no-wrap">
                    <q-input
                      v-model.number="props.row[props.col.name]"
                      type="number"
                      dense
                      borderless
                      hide-bottom-space
                      min="0"
                      step="1"
                      class="text-center narrow-input cell-input"
                      @update:model-value="(val) => onCellInput(props.row, props.col.name, val)"
                      @blur="onCellBlur(props.row, props.col.name)"
                    />
                  </div>
                  <div v-else-if="props.col.name.startsWith('day_')">
                    <q-input
                      v-if="!isFriday(props.col.name.replace('day_', ''))"
                      v-model.number="props.row[props.col.name]"
                      type="number"
                      dense
                      borderless
                      hide-bottom-space
                      min="0"
                      step="1"
                      class="text-center narrow-input cell-input"
                      @update:model-value="(val) => {
                        const d = props.col.name.replace('day_', '')
                        onDayPopupInput(props.row, d, val, getISOWeekInfo(d).weekKey)
                      }"
                      @blur="() => {
                        const d = props.col.name.replace('day_', '')
                        onDayPopupBlur(props.row, d, getISOWeekInfo(d).weekKey)
                      }"
                    />
                    <q-input
                      v-else
                      :model-value="computeFridayRemainder(props.row, getISOWeekInfo(props.col.name.replace('day_', '')).weekKey)"
                      type="number"
                      dense
                      borderless
                      hide-bottom-space
                      readonly
                      disable
                      class="text-center narrow-input cell-input"
                    />
                  </div>
                  <div v-else>
                    {{ props.value }}
                  </div>
                </q-td>
              </template>
            </q-table>
            </div>

            <!-- Нижняя панель: поиск и подсказки -->
            <div class="row items-center gap-2 w-full q-pa-md bg-grey-1">
              <div class="col-12 col-md-6">
                <q-input
                  ref="searchInputRef"
                  v-model="searchQuery"
                  label="Номенклатура (поиск: наименование / артикул / код)"
                  dense
                  clearable
                  @update:model-value="onInlineQueryChange"
                  @keydown.enter="onInlineEnter"
                >
                  <template #append>
                    <q-icon name="search" />
                  </template>
                </q-input>

                <q-menu
                  v-model="suggestOpen"
                  anchor="bottom left"
                  self="top left"
                  fit
                  max-height="300px"
                  :target="searchInputEl"
                  no-focus
                  @show="focusSearchInput"
                  @hide="focusSearchInput"
                >
                  <q-list dense style="min-width: 100%;">
                    <q-item
                      v-for="item in searchResults"
                      :key="item.item_code"
                      clickable
                      @click="addItemToPlan(item)"
                    >
                      <q-item-section>
                        <q-item-label>{{ item.item_name || '—' }}</q-item-label>
                        <q-item-label caption>
                          Арт. {{ item.item_article || '—' }} ({{ item.item_code }})
                          <span v-if="item.similarity && item.similarity < 1.0"> • схожесть: {{ Math.round(item.similarity * 100) }}%</span>
                        </q-item-label>
                      </q-item-section>
                    </q-item>

                    <q-item v-if="!searchResults.length">
                      <q-item-section class="text-grey-6">Ничего не найдено</q-item-section>
                    </q-item>
                  </q-list>
                </q-menu>
              </div>
            </div>

            <!-- Пагинация -->
            <div class="row justify-center q-pa-md">
              <q-pagination
                v-model="pagination.page"
                :max="totalPages"
                :max-pages="6"
                boundary-numbers
                direction-links
                @update:model-value="setPage"
              />
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { Notify } from 'quasar'
import api, { getPlanningAnchor, type PlanningAnchorResponse } from '../services/api'

/**
 * Типы
 */
interface PlanItem {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string
  month_plan: number  // используем как "План на квартал" для совместимости с UI
  days?: Record<string, number>
  [key: string]: any  // для динамических полей по неделям: week_{YYYY-Www}
}

interface SearchResult {
  item_id?: number
  item_code: string
  item_name: string
  item_article: string
  similarity?: number
}

/**
 * Состояние
 */
const router = useRouter()

const searchQuery = ref('')
const searchResults = ref<SearchResult[]>([])
const suggestOpen = ref(false)
const searchInputRef = ref()
const searchInputEl = computed(() => (searchInputRef.value && (searchInputRef.value as any).$el) || null)

const deletingId = ref<number | null>(null)
const currentPage = ref(1)
const totalItems = ref(0)
const totalPages = ref(1)
const loading = reactive({
  save: false,
  search: false,
  table: false
})

// Якорная дата окна плана: первый НЕ закрытый рабочий день после последнего закрытого.
// При отсутствии закрытий (процесс не начат) бэкенд вернёт previous_workday(today).
const planningAnchor = ref<PlanningAnchorResponse | null>(null)
const anchorDate = computed<string>(() => planningAnchor.value?.anchor_date || todayStr.value)

// Текущее загруженное окно (для операций типа delete_row, чтобы совпадало с UI)
const windowStart = ref<string>('')
const windowDays = ref<number>(0)

// Данные таблицы
const rowData = ref<PlanItem[]>([])

// Карта недель квартала: порядок и соответствующие «пятницы»
const weeksOrder = ref<string[]>([]) // ['2025-W01', '2025-W02', ...]
const weekToFriday = ref<Record<string, string>>({}) // key -> 'YYYY-MM-DD'
const weekToDates = ref<Record<string, string[]>>({}) // key -> ['YYYY-MM-DD', ...]
// Состояние разворачивания недель (динамические подполя-дни)
const expandedWeeks = ref<string[]>([])
function isWeekExpanded(weekKey: string): boolean {
  return expandedWeeks.value.includes(weekKey)
}
function toggleWeekExpand(weekKey: string): void {
  const idx = expandedWeeks.value.indexOf(weekKey)
  if (idx >= 0) expandedWeeks.value.splice(idx, 1)
  else expandedWeeks.value.push(weekKey)
}
// Текущая дата (ISO, локальная)
const todayStr = ref(toISODate(new Date()))

// Пагинация для QTable
const pagination = reactive({
  sortBy: 'item_name',
  descending: false,
  page: 1,
  rowsPerPage: 50,
  rowsNumber: 0
})

// Безопасный расчёт глобального номера строки с учётом пагинации и вариативности индексов слота
function computeRowNum(slotProps: any): number {
  const localIndex = Number((slotProps?.rowIndex ?? slotProps?.pageIndex ?? 0)) || 0
  const rpp = (pagination.rowsPerPage && pagination.rowsPerPage > 0)
    ? pagination.rowsPerPage
    : (pagination.rowsNumber || rowData.value.length || 0)
  const page = Number(pagination.page || 1)
  return (page - 1) * rpp + localIndex + 1
}

// Утилита фокуса поля поиска
function focusSearchInput() {
  nextTick(() => {
    try {
      const comp: any = searchInputRef.value
      if (comp && typeof comp.focus === 'function') {
        comp.focus()
        return
      }
      const el = comp?.$el?.querySelector?.('input')
      if (el) el.focus()
    } catch {}
  })
}

// Изменения для сохранения
let pendingChanges: Array<{
  item_id: number
  date: string // пятница ISO-недели
  qty: number
}> = []

/**
 * Вспомогательные функции ISO-недели
 */
function toISODate(d: Date): string {
  const yyyy = d.getFullYear()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd}`
}

function getISOUTCDay(d: Date): number {
  // Понедельник=1 ... Воскресенье=7
  const wd = d.getUTCDay()
  return wd === 0 ? 7 : wd
}

function getISOWeekInfo(dateStr: string): { weekKey: string; friday: string } {
  // Работать в UTC, чтобы исключить смещения
  const d = new Date(dateStr + 'T00:00:00Z')
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))
  const dayNum = getISOUTCDay(target)

  // Перенос к четвергу текущей недели, чтобы правильно определить год недели
  target.setUTCDate(target.getUTCDate() + (4 - dayNum))

  const weekYear = target.getUTCFullYear()
  const yearStart = new Date(Date.UTC(weekYear, 0, 1))
  const weekNo = Math.ceil((((target.getTime() - yearStart.getTime()) / 86400000) + 1) / 7)

  // Находим понедельник недели (возвращаемся от исходной даты к понедельнику)
  const monday = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))
  const dayNumOriginal = getISOUTCDay(monday)
  monday.setUTCDate(monday.getUTCDate() - (dayNumOriginal - 1)) // назад до понедельника

  // Пятница = понедельник + 4 дня
  const friday = new Date(monday)
  friday.setUTCDate(monday.getUTCDate() + 4)
  const fridayStr = toISODate(friday)

  const weekKey = `${weekYear}-W${String(weekNo).padStart(2, '0')}`
  return { weekKey, friday: fridayStr }
}

/**
 * Вспомогательные функции для недель/дней и распределения
 */
function isFriday(dateStr: string): boolean {
  const d = new Date(dateStr)
  // Пт = 5 (0=Вс)
  return d.getDay() === 5
}

function getFullWeekDays(weekKey: string): string[] {
  return weekToDates.value[weekKey] || []
}

function getVisibleWeekDays(weekKey: string): string[] {
  // In quarterly plan view we must not auto-shift/hide past days.
  // Always show full week (all 7 days) when expanded.
  return getFullWeekDays(weekKey)
}

function computeFridayRemainder(row: any, weekKey: string): number {
  const weekTarget = Number(row[`week_${weekKey}`] || 0)
  const days = getFullWeekDays(weekKey)
  let sumOther = 0
  for (const d of days) {
    if (isFriday(d)) continue
    sumOther += Number(row[`day_${d}`] || 0)
  }
  // Автоповышение включено по умолчанию
  if (sumOther >= weekTarget) return 0
  return Math.max(0, weekTarget - sumOther)
}

function recalcWeekAfterDayChange(row: any, weekKey: string): void {
  const days = getFullWeekDays(weekKey)
  const currentTarget = Number(row[`week_${weekKey}`] || 0)
  let sumOther = 0
  let friday = ''
  for (const d of days) {
    if (isFriday(d)) { friday = d; continue }
    sumOther += Number(row[`day_${d}`] || 0)
  }
  // Автоповышение недельного плана
  if (sumOther > currentTarget) {
    row[`week_${weekKey}`] = sumOther
    if (friday) {
      row[`day_${friday}`] = 0
      addPendingChange(row.item_id, friday, 0)
    }
  } else {
    const remainder = Math.max(0, currentTarget - sumOther)
    if (friday) {
      row[`day_${friday}`] = remainder
      addPendingChange(row.item_id, friday, remainder)
    }
  }
  // Пересчёт суммы квартала (по текущим week_* полям)
  let sumQ = 0
  weeksOrder.value.forEach(w => {
    sumQ += Number(row[`week_${w}`] || 0)
  })
  row.month_plan = sumQ
}

function onDayPopupInput(row: any, dateStr: string, val: any, weekKey: string): void {
  const qty = Number(val ?? row[`day_${dateStr}`] ?? 0) || 0
  row[`day_${dateStr}`] = qty
  if (!isFriday(dateStr)) {
    addPendingChange(row.item_id, dateStr, qty)
  }
  recalcWeekAfterDayChange(row, weekKey)
}

function onDayPopupBlur(row: any, dateStr: string, weekKey: string): void {
  const qty = Number(row[`day_${dateStr}`] || 0) || 0
  if (!isFriday(dateStr)) {
    addPendingChange(row.item_id, dateStr, qty)
  }
  recalcWeekAfterDayChange(row, weekKey)
}

// Окно квартального плана как скользящий горизонт: start + 3 календарных месяца.
function getRollingThreeMonthsBounds(startIso: string): { start: string; daysCount: number } {
  try {
    const start = new Date(startIso + 'T00:00:00')
    const endExclusive = new Date(start)
    endExclusive.setMonth(endExclusive.getMonth() + 3)
    const endInclusive = new Date(endExclusive)
    endInclusive.setDate(endInclusive.getDate() - 1)
    const daysCount = Math.max(1, Math.floor((endInclusive.getTime() - start.getTime()) / 86400000) + 1)
    return { start: startIso, daysCount }
  } catch {
    return { start: startIso, daysCount: 90 }
  }
}

/**
 * Колонки таблицы
 */
const columns = computed(() => {
  const cols: any[] = [
    {
      name: 'actions',
      label: '',
      align: 'center' as const,
      field: 'actions',
      sortable: false,
      classes: 'col-actions sticky-actions',
      headerClasses: 'col-actions sticky-actions'
    },
    {
      name: 'rownum',
      label: '#',
      align: 'right' as const,
      field: 'rownum',
      sortable: false,
      classes: 'col-rownum sticky-rownum',
      headerClasses: 'col-rownum sticky-rownum'
    },
    {
      name: 'item_name',
      required: true,
      label: 'Изделие',
      align: 'left' as const,
      field: 'item_name',
      sortable: true,
      classes: 'sticky-name',
      headerClasses: 'sticky-name'
    },
    {
      name: 'month_plan',
      label: 'План на квартал',
      align: 'right' as const,
      field: 'month_plan',
      sortable: true,
      format: (val: number) => val || 0
    }
  ]

  // Добавляем колонки по неделям текущего квартала (+ разворот на дни слева от недели)
  weeksOrder.value.forEach((wk) => {
    const friday = weekToFriday.value[wk]
    // Читаемая метка колонки: "Wxx пт dd.mm"
    let label = wk
    if (friday) {
      try {
        const f = new Date(friday as string)
        const dd = String(f.getDate()).padStart(2, '0')
        const mm = String(f.getMonth() + 1).padStart(2, '0')
        const ww = wk.split('W')[1] || ''
        label = `W${ww} пт ${dd}.${mm}`
      } catch { /* no-op */ }
    }

    // Если неделя развернута — сначала добавляем дни слева (для текущей недели — только видимые дни начиная с сегодня)
    if (isWeekExpanded(wk)) {
      const ds = getVisibleWeekDays(wk)
      ds.forEach((d) => {
        const dateObj = new Date(d)
        const header = dateObj.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', weekday: 'short' })
        cols.push({
          name: `day_${d}`,
          label: header,
          align: 'center' as const,
          field: `day_${d}`,
          sortable: false,
          classes: 'narrow-col',
          headerClasses: 'narrow-col',
          format: (val: number) => val || 0
        })
      })
    }

    // Сводная колонка недели (редактируемая) — после дней
    cols.push({
      name: `week_${wk}`,
      label,
      align: 'center' as const,
      field: `week_${wk}`,
      sortable: false,
      classes: 'narrow-col week-col-cell',
      headerClasses: 'narrow-col week-header',
      format: (val: number) => val || 0
    })
  })

  return cols
})

/**
 * Загрузка данных: берём помесячную матрицу за квартал и агрегируем по неделям
 */
async function loadPlanData() {
  try {
    loading.table = true

    // 1) Узнаём якорь окна плана с бэкенда (по закрытиям дня)
    try {
      planningAnchor.value = await getPlanningAnchor()
    } catch (e) {
      planningAnchor.value = null
    }

    // 2) Скользящее окно: от max(anchor_date, today) на 3 месяца вперёд.
    // Это исключает "залипание" на прошлом календарном квартале в начале нового месяца.
    const start = (anchorDate.value && anchorDate.value > todayStr.value) ? anchorDate.value : todayStr.value
    const bounds = getRollingThreeMonthsBounds(start)
    const daysNeeded = Math.max(1, bounds.daysCount)

    windowStart.value = start
    windowDays.value = daysNeeded

    const { data } = await api.post('/v1/plan/matrix', {
      start_date: start,
      days: daysNeeded,
      page: currentPage.value,
      page_size: 50,
      sort_by: 'item_name',
      sort_dir: 'asc'
    })

    const rows: PlanItem[] = data.rows || []
    const dates: string[] = data.dates || []

    // Собираем карту недель, их «пятниц» и принадлежность дат
    const mapFriday: Record<string, string> = {}
    const mapDates: Record<string, string[]> = {}
    const order: string[] = []

    for (const d of dates) {
      const { weekKey, friday } = getISOWeekInfo(d)
      if (!mapDates[weekKey]) {
        mapDates[weekKey] = []
        mapFriday[weekKey] = friday
        order.push(weekKey)
      }
      mapDates[weekKey].push(d)
    }

    weekToFriday.value = mapFriday
    weekToDates.value = mapDates

    // Keep all quarter weeks in chronological order (no hiding past weeks)
    order.sort((a, b) => {
      const da = (mapDates[a] || []).slice().sort()[0] || ''
      const db = (mapDates[b] || []).slice().sort()[0] || ''
      return da.localeCompare(db)
    })
    weeksOrder.value = order

    // Проецируем дни и агрегируем строки по неделям (только по weeksOrder)
    for (const row of rows) {
      // Преобразуем словарь days -> плоские ключи day_YYYY-MM-DD
      const daysMap = (row && row.days) ? (row.days as Record<string, number>) : {}
      Object.keys(mapDates).forEach(wk => {
        const ds = mapDates[wk] || []
        for (const d of ds) {
          const key = `day_${d}`
          if (row[key] === undefined) {
            row[key] = Number(daysMap?.[d] ?? 0)
          }
        }
      })

      // Агрегация по видимым неделям
      let sumQuarter = 0
      for (const wk of weeksOrder.value) {
        const ds = mapDates[wk] || []
        let s = 0
        for (const d of ds) {
          s += Number(row[`day_${d}`] || 0)
        }
        row[`week_${wk}`] = s
        sumQuarter += s
      }
      row.month_plan = sumQuarter
    }

    rowData.value = rows

    totalItems.value = data.total || 0
    totalPages.value = Math.ceil(totalItems.value / 50)

    // Обновляем пагинацию
    pagination.rowsNumber = totalItems.value
    pagination.page = currentPage.value
  } catch (error: any) {
    const message = error?.response?.data?.detail || 'Ошибка загрузки квартального плана'
    Notify.create({ type: 'negative', message })
  } finally {
    loading.table = false
  }
}

/**
 * Поиск изделий
 */
async function searchItems(query: string) {
  if (!query || query.length < 2) {
    searchResults.value = []
    suggestOpen.value = false
    return
  }

  try {
    loading.search = true
    const url = `/v1/nomenclature/search?q=${encodeURIComponent(query)}&limit=20`
    const { data } = await api.get(url)
    const items = Array.isArray(data?.items) ? data.items : []
    searchResults.value = items
    suggestOpen.value = !!searchQuery.value && searchQuery.value.length >= 2
  } catch (error: any) {
    const message = error?.response?.data?.detail || 'Ошибка поиска'
    Notify.create({ type: 'negative', message })
    searchResults.value = []
    suggestOpen.value = false
  } finally {
    loading.search = false
  }
}

// Обработчики поиска
function onInlineQueryChange() {
  searchItems(searchQuery.value)
}

function onInlineEnter() {
  if (searchResults.value.length > 0) {
    addItemToPlan(searchResults.value[0]!)
  }
}

async function addItemToPlan(item: SearchResult) {
  try {
    await api.post('/v1/plan/ensure_item', {
      item_code: item.item_code,
      item_name: item.item_name,
      item_article: item.item_article
    })

    Notify.create({ type: 'positive', message: `Добавлено: ${item.item_name}` })

    suggestOpen.value = false
    searchQuery.value = ''
    await loadPlanData()
  } catch (error: any) {
    const message = error?.response?.data?.detail || 'Ошибка добавления изделия'
    Notify.create({ type: 'negative', message })
  }
}

/**
 * Изменения и сохранение
 */
function addPendingChange(itemId: number, date: string, qty: number) {
  const key = `${itemId}|${date}`
  const existingIndex = pendingChanges.findIndex(change => `${change.item_id}|${change.date}` === key)

  if (existingIndex >= 0 && pendingChanges[existingIndex]) {
    pendingChanges[existingIndex].qty = qty
  } else {
    pendingChanges.push({ item_id: itemId, date, qty })
  }
}

async function onDeleteRow(row: PlanItem) {
  try {
    if (!row?.item_id) return
    const ok = window.confirm(`Удалить строку для: ${row.item_name || row.item_code}?`)
    if (!ok) return
    deletingId.value = row.item_id

    // Удаляем в пределах текущего отображаемого окна (чтобы совпадало с UI)
    const fallbackStart = todayStr.value
    const fallbackBounds = getRollingThreeMonthsBounds(fallbackStart)
    const start = windowStart.value || fallbackStart
    const daysCount = windowDays.value || fallbackBounds.daysCount
    await api.post('/v1/plan/delete_row', {
      item_id: row.item_id,
      start_date: start,
      days: daysCount
    })

    Notify.create({ type: 'positive', message: 'Строка удалена' })
    // Локально убираем строку
    rowData.value = rowData.value.filter(r => r.item_id !== row.item_id)
    totalItems.value = Math.max(0, (totalItems.value || 0) - 1)
    pagination.rowsNumber = Math.max(0, (pagination.rowsNumber || 0) - 1)
    // Фоновая перезагрузка
    loadPlanData()
  } catch (error: any) {
    const message = error?.response?.data?.detail || 'Ошибка удаления'
    Notify.create({ type: 'negative', message })
  } finally {
    deletingId.value = null
  }
}

async function saveChanges() {
  // Форсируем blur активного инпута, чтобы зафиксировать последнее редактирование
  try { (document.activeElement as HTMLElement)?.blur?.() } catch {}
  await nextTick()

  if (!pendingChanges.length) {
    Notify.create({ type: 'info', message: 'Нет изменений для сохранения' })
    return
  }

  try {
    loading.save = true
    const { data } = await api.post('/v1/plan/bulk_upsert', {
      entries: pendingChanges
    })

    Notify.create({
      type: 'positive',
      message: `Сохранено записей: ${data.saved || 0}`
    })

    pendingChanges = []
    await loadPlanData()
  } catch (error: any) {
    const message = error?.response?.data?.detail || 'Ошибка сохранения'
    Notify.create({ type: 'negative', message })
  } finally {
    loading.save = false
  }
}

/**
 * Обработчики таблицы
 */
function onRequest(props: any) {
  const { page, rowsPerPage, sortBy, descending } = props.pagination
  pagination.page = page
  pagination.rowsPerPage = rowsPerPage
  pagination.sortBy = sortBy
  pagination.descending = descending
  currentPage.value = page

  loadPlanData()
}

function setPage(page: number) {
  currentPage.value = Math.max(1, Math.min(page, totalPages.value))
  loadPlanData()
}
function onCellBlur(row: PlanItem, columnName: string) {
  if (!columnName.startsWith('week_')) return
  const wk = columnName.replace('week_', '')
  const qty = Number(row[columnName] || 0) || 0
  // Пользователь изменил недельный план (target)
  row[columnName] = qty
  // Пересчёт пятницы как остатка и обновление month_plan (с автоповышением)
  recalcWeekAfterDayChange(row as any, wk)
}

// Фиксация изменений по мере ввода
function onCellInput(row: PlanItem, columnName: string, value: string | number | null) {
  if (!columnName.startsWith('week_')) return
  const wk = columnName.replace('week_', '')
  const qty = Number(value ?? row[columnName] ?? 0) || 0
  // Обновляем недельный target
  row[columnName] = qty
  // Пересчитываем пятницу-остаток и сумму квартала; pending на пятницу ставится внутри recalc
  recalcWeekAfterDayChange(row as any, wk)
}

function openSpecification(row: PlanItem) {
  try {
    const qty = Math.max(1, Number(row.month_plan || 1))
    router.push({
      name: 'specification',
      query: { item_code: row.item_code, qty }
    })
  } catch (e) {
    // no-op
  }
}

/**
 * Инициализация
 */
onMounted(() => {
  loadPlanData()
})
</script>

<style scoped>
.control-bar {
  position: relative;
  z-index: 35;
  margin-bottom: 12px;
}

/* Левые фиксированные колонки */
.production-plan-table :deep(th.col-actions),
.production-plan-table :deep(td.col-actions) {
  width: 56px;
  min-width: 56px;
  max-width: 56px;
}
.production-plan-table :deep(th.col-rownum),
.production-plan-table :deep(td.col-rownum) {
  width: 44px;
  min-width: 44px;
  max-width: 44px;
}
.production-plan-table :deep(th.sticky-actions),
.production-plan-table :deep(td.sticky-actions) {
  position: sticky;
  left: 0;
  z-index: 30; /* выше остальных */
  background: #fff;
}
.production-plan-table :deep(th.sticky-rownum),
.production-plan-table :deep(td.sticky-rownum) {
  position: sticky;
  left: 56px; /* width of actions column */
  z-index: 29;
  background: #fff;
}
.production-plan-table :deep(th.sticky-name),
.production-plan-table :deep(td.sticky-name) {
  position: sticky;
  left: 100px; /* width of actions (56px) + rownum (44px) */
  z-index: 28; /* сразу под .sticky-rownum */
  width: 360px;
  min-width: 360px;
  max-width: 360px;
  background: #fff;
  box-shadow: 1px 0 0 rgba(0, 0, 0, 0.12);
}

/* Горизонтальный скролл снизу */
.h-scroll {
  overflow-x: auto;
  overflow-y: visible; /* ВАЖНО: не скрывать вертикаль, иначе sticky не работает */
  width: 100%;
  scrollbar-gutter: stable both-edges;
}
.h-scroll-inner {
  display: inline-block;   /* позволяет ширине контента определять горизонталь */
  min-width: 100%;
}

/* Вертикальный скролл-обертка для sticky top/left */
.v-scroll {
  overflow-y: auto;
  height: 70vh;
  width: 100%;
  position: relative;
  overscroll-behavior: contain;
}

/* Контент таблицы — ширина по содержимому */
.production-plan-table.wide-content { width: max-content; }
.production-plan-table :deep(table) {
  width: max-content;
  border-collapse: separate;
  border-spacing: 0;
}

/* Единый контейнер прокрутки для таблицы (вертикальная + горизонтальная) */
.table-scroll {
  max-height: 70vh;
  overflow: auto;
  width: 100%;
  position: relative;
  scrollbar-gutter: stable both-edges;
}
:deep(.q-table) { width: max-content; }
/* Явный класс для table-class */
.wide-table {
  width: max-content;
  white-space: nowrap;
}

/* Кастомизация скролл-бара (необязательно) */
.h-scroll::-webkit-scrollbar { height: 10px; }
.h-scroll::-webkit-scrollbar-thumb { background-color: rgba(0,0,0,0.25); border-radius: 5px; }
.h-scroll::-webkit-scrollbar-track { background-color: rgba(0,0,0,0.05); }

/* Узкие колонки для недель/дней и компактные инпуты */
.production-plan-table :deep(th.narrow-col),
.production-plan-table :deep(td.narrow-col) {
  width: 80px;
  min-width: 60px;
  max-width: 110px;
  padding-left: 4px;
  padding-right: 4px;
}
.production-plan-table :deep(.narrow-input) {
  max-width: 70px;
  min-width: 50px;
}
.production-plan-table :deep(.narrow-input .q-field__control) {
  min-height: 18px;
  height: 18px;
  padding-left: 0;
  padding-right: 0;
}
.production-plan-table :deep(.narrow-input .q-field__native) {
  text-align: center;
  padding-top: 0;
  padding-bottom: 0;
  font-size: 11px;
  line-height: 1;
}

/* Отключаем внутренний горизонтальный скролл QTable, чтобы sticky-колонки работали с внешним .h-scroll */
.production-plan-table :deep(.q-table__middle) { overflow: visible !important; }
.production-plan-table :deep(.q-table__container) { overflow: visible; }

/* Выделение недельных колонок с кнопкой разворота */
.production-plan-table :deep(th.week-header) {
  background: #e3f2fd;         /* светло-голубой фон */
  color: #0d47a1;               /* тёмно-синий текст */
  border-bottom: 1px solid #90caf9;
  font-weight: 600;
}
.production-plan-table :deep(td.week-col-cell) {
  background: #f7fbff;          /* очень светлый фон для согласованности с шапкой */
}
.production-plan-table :deep(th.week-header .row) { flex-wrap: nowrap; gap: 4px; }
.production-plan-table :deep(th.week-header .q-btn) { min-width: 24px; }

/* Sticky header (фиксируем шапку таблицы при вертикальной прокрутке) */
.production-plan-table :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 22; /* выше строк данных */
  background: #eef3f9;
  color: #1f2a37;
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.02em;
  border-bottom: 1px solid #d4dde8;
}
/* Усиление для заголовков недель: остаются цветными и тоже фиксируются */
.production-plan-table :deep(th.week-header) {
  position: sticky;
  top: 0;
  z-index: 23;
}

/* Усиление фиксации угловых заголовочных ячеек (пересечение слева/сверху) */
.production-plan-table :deep(thead th.sticky-actions) {
  top: 0;
  z-index: 31; /* выше остальных заголовков */
  background: #eef3f9;
}
.production-plan-table :deep(thead th.sticky-rownum) {
  top: 0;
  z-index: 30; /* сразу под .sticky-actions */
  background: #eef3f9;
}
.production-plan-table :deep(thead th.sticky-name) {
  top: 0;
  z-index: 29; /* сразу под .sticky-rownum */
  background: #eef3f9;
}

.production-plan-table :deep(tbody td) {
  padding: 1px 4px;
  min-height: 20px;
  border-bottom: 1px solid #edf1f6;
  line-height: 1;
}
.production-plan-table :deep(tbody tr:nth-child(even) td) {
  background: #fafcff;
}
.production-plan-table :deep(tbody tr:hover td) {
  background: #eef6ff;
}

.actions-cell {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1px;
  min-height: 16px;
}

.compact-action-btn {
  opacity: 0.85;
  min-width: 16px;
  width: 16px;
  height: 16px;
  padding: 0;
}

.compact-action-btn:hover {
  opacity: 1;
}

.item-name-cell {
  max-width: 360px;
}

.item-name-full {
  display: inline-block;
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.1;
  vertical-align: bottom;
}

/* Ячейки ввода как обычная табличная сетка (без underline/спиннеров) */
.production-plan-table :deep(.cell-input .q-field__control) {
  border-radius: 0;
  background: transparent;
  border: 0 !important;
  box-shadow: none !important;
}
.production-plan-table :deep(.cell-input .q-field__native),
.production-plan-table :deep(.cell-input input) {
  background: transparent !important;
  min-height: 18px;
  height: 18px;
  line-height: 1;
  padding: 0;
}
.production-plan-table :deep(.cell-input input[type='number']) {
  -moz-appearance: textfield;
  appearance: textfield;
}
.production-plan-table :deep(.cell-input input[type='number']::-webkit-outer-spin-button),
.production-plan-table :deep(.cell-input input[type='number']::-webkit-inner-spin-button) {
  -webkit-appearance: none;
  margin: 0;
}
</style>
