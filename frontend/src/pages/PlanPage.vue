<template>
  <q-page class="q-pa-lg">
    <div class="row justify-center">
      <div class="col-12">
        <q-card>
          <q-card-section>
            <div class="text-h5">План выпуска техники дневной</div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <!-- Панель управления -->
            <div class="row items-center gap-2 w-full mb-4 flex-wrap">

              <!-- read-only mode: save disabled per spec -->

              <!-- Горизонт дат -->


            </div>


            <!-- Таблица плана -->
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
              <!-- Заголовки с применением headerClasses для корректной sticky-фиксации -->
              <template v-slot:header-cell="hprops">
                <q-th :props="hprops" :class="hprops.col.headerClasses">
                  {{ hprops.col.label }}
                </q-th>
              </template>

              <!-- Кастомные слоты для редактируемых ячеек -->
              <template v-slot:body-cell="props">
                <q-td :props="props">
                  <div v-if="props?.col?.name === 'actions'">
                    <q-btn
                      flat
                      round
                      dense
                      icon="account_tree"
                      color="primary"
                      class="q-mr-xs"
                      @click="openSpecification(props.row)"
                    />
                    <q-btn
                      flat
                      round
                      dense
                      icon="delete"
                      color="negative"
                      :loading="deletingId === props.row.item_id"
                      :disable="deletingId === props.row.item_id"
                      @click="onDeleteRow(props.row)"
                    />
                  </div>
                  <div v-else-if="props?.col?.name === 'rownum'">
                    <div class="text-right">
                      {{ computeRowNum(props) }}
                    </div>
                  </div>
                  <div v-else-if="props?.col?.name?.startsWith('day_')">
                    <div class="text-center">{{ props.row[props.col?.name] || 0 }}</div>
                  </div>
                  <div v-else>
                    {{ props.value }}
                  </div>
                </q-td>
              </template>

           </q-table>
           </div>

           <!-- Нижняя панель с инлайн-подсказками -->
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
             <div class="col-6 col-md-2">
               <q-input
                 v-model.number="horizonDays"
                 label="Горизонт, дней"
                 type="number"
                 dense
                 min="1"
                 max="90"
                 step="1"
               />
             </div>
             <div class="col-auto">
               <q-btn color="primary" text-color="white" label="Применить" @click="applyHorizon" />
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
import api from '../services/api'

// Типы
interface PlanItem {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string
  month_plan: number
  days: Record<string, number>
  [key: string]: any  // Для динамических полей по дням
}

interface SearchResult {
  item_id?: number
  item_code: string
  item_name: string
  item_article: string
  similarity?: number
}

// Состояние
const router = useRouter()

const searchQuery = ref('')
const searchResults = ref<SearchResult[]>([])
const suggestOpen = ref(false)
const searchInputRef = ref()
const searchInputEl = computed(() => (searchInputRef.value && (searchInputRef.value as any).$el) || null)
const horizonDays = ref(30)
const deletingId = ref<number | null>(null)
const currentPage = ref(1)
const totalItems = ref(0)
const totalPages = ref(1)
const loading = reactive({
  save: false,
  search: false,
  table: false
})

// Данные таблицы
const rowData = ref<PlanItem[]>([])
const dates = ref<string[]>([])
const todayStr = ref<string>(new Date().toISOString().slice(0, 10))

// Выходные/праздничные дни (базовый набор РФ; без переносов)
const HOLIDAYS_MD = new Set<string>([
  '01-01','01-02','01-03','01-04','01-05','01-06','01-07','01-08', // Новогодние каникулы
  '02-23', // День защитника Отечества
  '03-08', // Международный женский день
  '05-01', // Праздник Весны и Труда
  '05-09', // День Победы
  '06-12', // День России
  '11-04'  // День народного единства
])

function isWeekend(dateStr: string): boolean {
  // Вс = 0, Сб = 6
  const d = new Date(dateStr)
  const wd = d.getDay()
  return wd === 0 || wd === 6
}

function isHoliday(dateStr: string): boolean {
  const md = dateStr.slice(5) // MM-DD
  return HOLIDAYS_MD.has(md)
}

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
  try {
    const idxFromSlot = (slotProps?.rowIndex ?? slotProps?.pageIndex)
    let localIndex = (typeof idxFromSlot === 'number') ? idxFromSlot : rowData.value.indexOf(slotProps?.row)
    if (localIndex == null || localIndex < 0) localIndex = 0
    const rppFallback = (pagination?.rowsNumber || rowData.value.length || 1)
    const rpp = (pagination?.rowsPerPage && pagination.rowsPerPage > 0) ? pagination.rowsPerPage : rppFallback
    const page = Number(pagination?.page || 1)
    return (page - 1) * rpp + Number(localIndex) + 1
  } catch {
    return 1
  }
}

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

// Глобальное состояние для отслеживания изменений
let pendingChanges: Array<{
  item_id: number
  date: string
  qty: number
}> = []

// Вычисляемые свойства для QTable
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
      name: 'item_article',
      label: 'Артикул',
      align: 'left' as const,
      field: 'item_article',
      sortable: true
    },
    {
      name: 'month_plan',
      label: 'План на месяц',
      align: 'right' as const,
      field: 'month_plan',
      sortable: true,
      format: (val: number) => val || 0
    }
  ]

  // Добавляем колонки по дням
  dates.value.forEach(dateStr => {
    const date = new Date(dateStr)
    const header = date.toLocaleDateString('ru-RU', {
      day: '2-digit',
      month: '2-digit'
    })

    const isW = isWeekend(dateStr)
    const isH = isHoliday(dateStr)
    const headerClass = isH ? 'holiday-col' : (isW ? 'weekend-col' : '')
    const cellClass = isH ? 'holiday-cell' : (isW ? 'weekend-cell' : '')

    cols.push({
      name: `day_${dateStr}`,
      label: header,
      align: 'center' as const,
      field: `day_${dateStr}`,
      sortable: false,
      classes: cellClass,
      headerClasses: headerClass,
      format: (val: number) => val || 0
    })
  })

  return cols
})

// Загрузка данных
async function loadPlanData() {
  try {
    loading.table = true
    const { data } = await api.post('/v1/plan/matrix', {
      start_date: new Date().toISOString().split('T')[0],
      days: horizonDays.value,
      page: currentPage.value,
      page_size: 50,
      sort_by: 'item_name',
      sort_dir: 'asc'
    })

    const rowsRaw: PlanItem[] = data.rows || []
    const allDates: string[] = data.dates || []
 
    // Оставляем только будущие даты (включая сегодня)
    const futureDates = allDates.filter(d => d >= todayStr.value)
 
    // Проецируем значения day_* для будущих дат
    const rowsProj = rowsRaw.map((row) => {
      const daysMap = (row && row.days) ? row.days as Record<string, number> : {}
      for (const d of futureDates) {
        const key = `day_${d}`
        ;(row as any)[key] = Number(daysMap?.[d] ?? 0)
      }
      return row
    })
 
    // Оставляем только те даты, где есть ненулевые значения хотя бы в одной строке
    const nonZeroDates = futureDates.filter(d => rowsProj.some(r => Number((r as any)[`day_${d}`] || 0) > 0))
 
    // Фильтруем строки: только с ненулевыми значениями в видимых датах
    const rowsFiltered = rowsProj.filter(r => nonZeroDates.some(d => Number((r as any)[`day_${d}`] || 0) > 0))
 
    // Пересчёт month_plan по видимым датам
    for (const r of rowsFiltered) {
      let sum = 0
      for (const d of nonZeroDates) sum += Number((r as any)[`day_${d}`] || 0)
      ;(r as any).month_plan = sum
    }
 
    rowData.value = rowsFiltered
    dates.value = nonZeroDates
 
    totalItems.value = rowsFiltered.length
    totalPages.value = Math.ceil(totalItems.value / 50)

    // Обновляем пагинацию
    pagination.rowsNumber = totalItems.value
    pagination.page = currentPage.value
  } catch (error: any) {
    const message = error?.response?.data?.detail || 'Ошибка загрузки плана'
    Notify.create({ type: 'negative', message })
  } finally {
    loading.table = false
  }
}

// Поиск изделий
async function searchItems(query: string) {
  if (!query || query.length < 2) {
    searchResults.value = []
    suggestOpen.value = false
    return
  }

  try {
    loading.search = true
    const url = `/v1/nomenclature/search?q=${encodeURIComponent(query)}&limit=20`
    console.log('PlanPage.search:url', url)
    const { data } = await api.get(url)
    console.log('PlanPage.search:resp', data)
    const items = Array.isArray(data?.items) ? data.items : []
    searchResults.value = items
    suggestOpen.value = !!searchQuery.value && searchQuery.value.length >= 2
  } catch (error: any) {
    console.error('PlanPage.search:error', error?.response?.data || error)
    const message = error?.response?.data?.detail || 'Ошибка поиска'
    Notify.create({ type: 'negative', message })
    searchResults.value = []
    suggestOpen.value = false
  } finally {
    loading.search = false
  }
}

// Обработчики событий
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

function addPendingChange(itemId: number, date: string, qty: number) {
  const key = `${itemId}|${date}`
  const existingIndex = pendingChanges.findIndex(change =>
    `${change.item_id}|${change.date}` === key
  )

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
    await api.post('/v1/plan/delete_row', {
      item_id: row.item_id,
      start_date: new Date().toISOString().split('T')[0],
      days: horizonDays.value
    })
    Notify.create({ type: 'positive', message: 'Строка удалена' })
    // Мгновенно скрываем строку локально, чтобы не ждать повторной загрузки
    rowData.value = rowData.value.filter(r => r.item_id !== row.item_id)
    totalItems.value = Math.max(0, (totalItems.value || 0) - 1)
    pagination.rowsNumber = Math.max(0, (pagination.rowsNumber || 0) - 1)
    // Перезагрузка данных для консистентности (фоново)
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

function applyHorizon() {
  currentPage.value = 1
  loadPlanData()
}

function setPage(page: number) {
  currentPage.value = Math.max(1, Math.min(page, totalPages.value))
  loadPlanData()
}


// Обработчики для QTable
function onRequest(props: any) {
  const { page, rowsPerPage, sortBy, descending } = props.pagination
  currentPage.value = page
  pagination.rowsPerPage = rowsPerPage
  pagination.sortBy = sortBy
  pagination.descending = descending

  loadPlanData()
}

function onCellBlur(row: PlanItem, columnName: string) {
  if (columnName.startsWith('day_')) {
    const date = columnName.replace('day_', '')
    const qty = row[columnName] || 0

    // Обновляем сумму месяца
    let sum = 0
    dates.value.forEach(d => {
      sum += row[`day_${d}`] || 0
    })
    row.month_plan = sum

    // Добавляем в pending changes
    addPendingChange(row.item_id, date, qty)
  }
}

// Фиксация изменений по мере ввода, чтобы данные не терялись при сохранении
function onCellInput(row: PlanItem, columnName: string, value: string | number | null) {
  if (!columnName.startsWith('day_')) return
  const date = columnName.replace('day_', '')
  const qty = Number(value ?? row[columnName] ?? 0) || 0
  row[columnName] = qty

  // Пересчёт суммы месяца
  let sum = 0
  dates.value.forEach(d => {
    sum += Number(row[`day_${d}`] || 0)
  })
  row.month_plan = sum

  // Кладём изменение в очередь на сохранение
  addPendingChange(row.item_id, date, qty)
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

// Инициализация
onMounted(() => {
  loadPlanData()
})
  
</script>

<style scoped>
.ag-theme-alpine {
  --ag-foreground-color: #213547;
  --ag-background-color: #ffffff;
  --ag-header-foreground-color: #ffffff;
  --ag-header-background-color: #1976d2;
  --ag-odd-row-background-color: #f5f5f5;
  --ag-row-hover-color: #e3f2fd;
}

/* Левые фиксированные колонки */
.production-plan-table :deep(th.col-actions),
.production-plan-table :deep(td.col-actions) {
  width: 88px;
  min-width: 88px;
  max-width: 88px;
}
.production-plan-table :deep(th.col-rownum),
.production-plan-table :deep(td.col-rownum) {
  width: 48px;
  min-width: 48px;
  max-width: 48px;
}
.production-plan-table :deep(th.sticky-actions),
.production-plan-table :deep(td.sticky-actions) {
  position: sticky;
  left: 0;
  z-index: 30; /* выше всех */
  background: #fff;
}
.production-plan-table :deep(th.sticky-rownum),
.production-plan-table :deep(td.sticky-rownum) {
  position: sticky;
  left: 88px; /* ширина колонки действий */
  z-index: 29;
  background: #fff;
}
.production-plan-table :deep(th.sticky-name),
.production-plan-table :deep(td.sticky-name) {
  position: sticky;
  left: 136px; /* width of actions (88px) + rownum (48px) */
  z-index: 28;
  background: #fff;
  box-shadow: 1px 0 0 rgba(0, 0, 0, 0.12);
}

/* Выделение выходных/праздников */
.production-plan-table :deep(th.weekend-col) {
  background: #f2f2f2;
}
.production-plan-table :deep(td.weekend-cell) {
  background: #fafafa;
}
.production-plan-table :deep(th.holiday-col) {
  background: #ffe6e6;
  color: #b71c1c;
}
.production-plan-table :deep(td.holiday-cell) {
  background: #fff2f2;
}

/* Горизонтальная прокрутка */
.h-scroll {
  overflow-x: auto;
  overflow-y: visible;
  width: 100%;
  scrollbar-gutter: stable both-edges;
}
.h-scroll-inner {
  display: inline-block;
  min-width: 100%;
}

/* Контент таблицы — ширина по содержимому */
.production-plan-table.wide-content { width: max-content; }
.production-plan-table :deep(table) {
  width: max-content;
  border-collapse: separate;
  border-spacing: 0;
}
:deep(.q-table) { width: max-content; }
.wide-table { width: max-content; white-space: nowrap; }

/* Отключаем внутренние overflow на контейнерах Quasar */
.production-plan-table :deep(.q-table__middle) { overflow: visible !important; }
.production-plan-table :deep(.q-table__container) { overflow: visible; }

/* Вертикальная прокрутка для области таблицы */
.v-scroll {
  overflow-y: auto;
  height: 70vh;
  width: 100%;
  position: relative;
  overscroll-behavior: contain;
}

/* Sticky header таблицы (оглавление колонок) */
.production-plan-table :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 22; /* выше строк данных и слева фиксированных ячеек */
  background: #fff;
}
/* Доп. явное правило для выделенных заголовков */
.production-plan-table :deep(th.weekend-col),
.production-plan-table :deep(th.holiday-col) {
  position: sticky;
  top: 0;
  z-index: 23;
  background: inherit;
}
/* Единый контейнер прокрутки для таблицы (вертикальная + горизонтальная) */
.table-scroll {
  max-height: 70vh;
  overflow: auto;
  width: 100%;
  position: relative;
  scrollbar-gutter: stable both-edges;
}
/* Усиление фиксации угловых заголовочных ячеек (левая шапка) */
.production-plan-table :deep(thead th.sticky-actions) {
  position: sticky;
  top: 0;
  left: 0;
  z-index: 31;
  background: #fff;
}
.production-plan-table :deep(thead th.sticky-rownum) {
  position: sticky;
  top: 0;
  left: 88px; /* ширина колонки действий */
  z-index: 30;
  background: #fff;
}
.production-plan-table :deep(thead th.sticky-name) {
  position: sticky;
  top: 0;
  left: 136px; /* actions (88px) + rownum (48px) */
  z-index: 29;
  background: #fff;
}
</style>