<template>
  <q-page class="q-pa-lg">
    <div class="row justify-center">
      <div class="col-12">
        <q-card>
          <q-card-section>
            <div class="row items-center justify-between q-gutter-sm">
              <div>
                <div class="text-h5">Отчёт о выпуске техники недельный</div>
                <div class="text-caption text-grey-7">
                  Неделя Пн–Вс. Факт за закрытый день — только для чтения.
                </div>
              </div>

              <div class="row items-center q-gutter-sm">
                <q-btn
                  outline
                  icon="chevron_left"
                  label="Пред. неделя"
                  :disable="loading.page || loading.save || loading.close"
                  @click="goWeek(-7)"
                />
                <q-btn
                  outline
                  icon="chevron_right"
                  label="След. неделя"
                  :disable="loading.page || loading.save || loading.close"
                  @click="goWeek(7)"
                />
              </div>
            </div>
          </q-card-section>

          <q-separator />

          <q-card-section>
            <div class="row items-center q-col-gutter-md q-row-gutter-sm">
              <div class="col-12 col-md-3">
                <q-input
                  v-model="anyDate"
                  label="Дата внутри недели"
                  dense
                  outlined
                  mask="####-##-##"
                  placeholder="YYYY-MM-DD"
                  :error="Boolean(anyDate) && !isValidISODate(anyDate)"
                  error-message="Формат даты: YYYY-MM-DD"
                  @keyup.enter="onLoadClick"
                />
              </div>
              <div class="col-auto row items-end">
                <q-btn
                  color="primary"
                  label="Загрузить"
                  class="load-btn-align"
                  :loading="loading.page"
                  :disable="!isValidISODate(anyDate) || loading.save || loading.close"
                  @click="onLoadClick"
                />
              </div>
              <div class="col-12 col-md-3">
                <q-input
                  v-model="selectedCloseDate"
                  label="Закрываемая дата"
                  dense
                  outlined
                  mask="####-##-##"
                  placeholder="YYYY-MM-DD"
                  :error="Boolean(selectedCloseDate) && !isCloseDateInWeek"
                  error-message="Дата должна быть из текущей недели отчёта"
                />
              </div>

              <div class="col-12 col-md-6">
                <q-banner v-if="closeHint" dense class="bg-grey-2">
                  <div>
                    <div>
                      <b>Закрываемый день:</b> {{ closeHint.close_date }}
                      <span class="text-grey-7">→ перенос на</span>
                      <b>{{ closeHint.target_date }}</b>
                    </div>
                    <div class="text-caption text-grey-7">
                      Повторное закрытие допускается (re-run): перенос будет пересчитан.
                    </div>
                  </div>
                </q-banner>
              </div>

              <div class="col-12">
                <div class="row items-center q-gutter-sm">
                  <q-btn
                    color="positive"
                    label="Сохранить факт"
                    :loading="loading.save"
                    :disable="!pendingFacts.length || loading.page || loading.close"
                    @click="save"
                  />
                  <q-btn
                    color="negative"
                    outline
                    label="Закрыть день"
                    :loading="loading.close"
                    :disable="!canCloseDay"
                    @click="closeDay"
                  />
                  <div class="text-caption text-grey-7">
                    Изменений: {{ pendingFacts.length }}
                  </div>
                </div>
              </div>
            </div>

            <div class="table-scroll q-mt-md">
              <q-table
                :rows="rows"
                :columns="columns"
                row-key="item_id"
                flat
                :loading="loading.page"
                class="production-report-table"
                table-class="wide-table"
                :table-style="{ width: 'max-content', whiteSpace: 'nowrap' }"
                :wrap-cells="false"
                hide-pagination
                :rows-per-page-options="[0]"
              >
                <template v-slot:header-cell="hprops">
                  <q-th :props="hprops" :class="hprops.col.headerClasses">
                    <div class="col-header">
                      <div>{{ hprops.col.label }}</div>

                      <div
                        v-if="hprops.col?.name?.startsWith('day_') && dayMetaMap[hprops.col.dateKey]"
                        class="text-caption text-grey-7"
                      >
                        <div v-if="String(dayMetaMap[hprops.col.dateKey]?.close_status || '') === 'CLOSED'">
                          закрыто: план {{ fmtNum(dayMetaMap[hprops.col.dateKey]?.closed_planned) }},
                          факт {{ fmtNum(dayMetaMap[hprops.col.dateKey]?.closed_fact) }},
                          перенос {{ fmtNum(dayMetaMap[hprops.col.dateKey]?.carry_qty) }}
                        </div>
                      </div>
                    </div>
                  </q-th>
                </template>

                <template v-slot:body-cell="props">
                  <q-td :props="props" :class="cellClass(props)">
                    <div v-if="props.col.name === 'item_name'">
                      <div class="text-weight-medium item-name-line">
                        <span class="item-name-full">{{ props.row.item_name }}</span>
                      </div>
                      <div class="text-caption text-grey-7">
                        {{ props.row.item_article || '—' }} · {{ props.row.item_code }}
                      </div>
                    </div>

                    <div v-else-if="props.col.name === 'remaining_week'" class="text-right">
                      {{ fmtNum(props.value) }}
                    </div>

                    <div v-else-if="props.col.name === 'plan_week'" class="text-right">
                      {{ fmtNum(props.value) }}
                    </div>

                    <div v-else-if="props.col.name === 'fact_week'" class="text-right">
                      {{ fmtNum(props.value) }}
                    </div>

                    <div v-else-if="props.col.name.startsWith('day_')" class="day-cell">
                      <div class="text-caption text-grey-7">план: {{ fmtNum(getPlan(props.row, props.col.dateKey)) }}</div>

                      <div
                        v-if="getCarry(props.row, props.col.dateKey) > 0"
                        class="text-caption text-blue-8"
                      >
                        перенос: {{ fmtNum(getCarry(props.row, props.col.dateKey)) }}
                      </div>

                      <div
                        v-if="(getClosedPlan(props.row, props.col.dateKey) > 0) || (getClosedFact(props.row, props.col.dateKey) > 0)"
                        class="text-caption text-grey-7"
                      >
                        закрыто: план {{ fmtNum(getClosedPlan(props.row, props.col.dateKey)) }},
                        факт {{ fmtNum(getClosedFact(props.row, props.col.dateKey)) }}
                      </div>

                      <q-input
                        v-model.number="props.row[props.col.name]"
                        type="number"
                        dense
                        borderless
                        hide-bottom-space
                        min="0"
                        step="1"
                        class="fact-input cell-input"
                        :readonly="isDateClosed(props.col.dateKey)"
                        :disable="isDateClosed(props.col.dateKey)"
                        @update:model-value="(val) => onFactInput(props.row, props.col.dateKey, val)"
                      />
                      <div v-if="isOverProduced(props.row, props.col.dateKey)" class="text-caption text-negative">
                        перевыпуск
                      </div>
                    </div>

                    <div v-else>
                      {{ props.value }}
                    </div>
                  </q-td>
                </template>
              </q-table>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>
  </q-page>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Notify } from 'quasar'
import {
  bulkUpsertProductionReportFact,
  closeProductionReportDay,
  getProductionReportWeek,
  type ProductionReportWeekDay,
  type ProductionReportWeekRow
} from '../services/api'

type RowVM = ProductionReportWeekRow & {
  // dynamic fact inputs: fact_{date}
  [key: string]: any
}

const anyDate = ref<string>(new Date().toISOString().slice(0, 10))

const loading = ref({ page: false, save: false, close: false })
const days = ref<ProductionReportWeekDay[]>([])
const rows = ref<RowVM[]>([])
const closeHint = ref<{ today: string; close_date: string; target_date: string } | null>(null)
const selectedCloseDate = ref<string>('')
let loadSeq = 0

// Track pending changes (dedupe by item_id|date)
const pendingFacts = ref<Array<{ item_id: number; date: string; fact_qty: number }>>([])

function isValidISODate(v: string): boolean {
  const s = String(v || '').trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false
  const d = new Date(`${s}T00:00:00`)
  if (Number.isNaN(d.getTime())) return false
  const [y, m, day] = s.split('-').map(Number)
  return d.getFullYear() === y && (d.getMonth() + 1) === m && d.getDate() === day
}

function dateShift(isoDate: string, deltaDays: number): string {
  const d = new Date(`${isoDate}T00:00:00`)
  d.setDate(d.getDate() + deltaDays)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function fmtNum(v: any): string {
  const n = Number(v ?? 0)
  if (!isFinite(n)) return '0'
  return String(Math.round(n * 1000) / 1000)
}

function dayLabel(dateStr: string): string {
  try {
    const d = new Date(dateStr)
    const wd = d.toLocaleDateString('ru-RU', { weekday: 'short' })
    const dm = d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })
    return `${wd.replace('.', '')} ${dm}`
  } catch {
    return dateStr
  }
}

function isWeekend(dateStr: string): boolean {
  const d = new Date(dateStr)
  const wd = d.getDay() // 0=Sun
  return wd === 0 || wd === 6
}

const closeStatusMap = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  for (const d of days.value || []) {
    if (d?.date) m[d.date] = String(d.close_status || '')
  }
  return m
})

const dayMetaMap = computed<Record<string, any>>(() => {
  const m: Record<string, any> = {}
  for (const d of days.value || []) {
    if (d?.date) m[String(d.date)] = d
  }
  return m
})

const dayDateSet = computed<Set<string>>(() => new Set((days.value || []).map(d => String(d.date || ''))))
const isCloseDateInWeek = computed<boolean>(() => {
  if (!isValidISODate(selectedCloseDate.value)) return false
  return dayDateSet.value.has(selectedCloseDate.value)
})
const canCloseDay = computed<boolean>(() => {
  return !loading.value.page
    && !loading.value.save
    && !loading.value.close
    && pendingFacts.value.length === 0
    && isCloseDateInWeek.value
})

const rerunEditableDate = computed<string | null>(() => {
  // Backend allows re-run corrections for explicitly selected close day.
  return selectedCloseDate.value || null
})

function isDateClosed(dateStr: string): boolean {
  // CLOSED days are read-only, except current close day (re-run flow)
  if (rerunEditableDate.value && dateStr === rerunEditableDate.value) return false
  return closeStatusMap.value[dateStr] === 'CLOSED'
}

function getPlan(row: RowVM, dateStr: string): number {
  return Number(row?.plan_by_day?.[dateStr] ?? 0) || 0
}

function getFact(row: RowVM, dateStr: string): number {
  return Number(row?.fact_by_day?.[dateStr] ?? 0) || 0
}

function getCarry(row: RowVM, dateStr: string): number {
  return Number((row as any)?.carry_by_day?.[dateStr] ?? 0) || 0
}

function getClosedPlan(row: RowVM, dateStr: string): number {
  return Number((row as any)?.closed_plan_by_day?.[dateStr] ?? 0) || 0
}

function getClosedFact(row: RowVM, dateStr: string): number {
  return Number((row as any)?.closed_fact_by_day?.[dateStr] ?? 0) || 0
}

function isOverProduced(row: RowVM, dateStr: string): boolean {
  return getFact(row, dateStr) > getPlan(row, dateStr)
}

function projectFacts() {
  const ds = (days.value || []).map(d => d.date)
  rows.value = (rows.value || []).map((r) => {
    ;(r as any).__initialFacts = {}
    for (const d of ds) {
      const key = `fact_${d}`
      const value = Number(r.fact_by_day?.[d] ?? 0) || 0
      r[key] = value
      ;(r as any).__initialFacts[d] = value
    }
    return r
  })
}

async function load(preferCloseWeek: boolean = false) {
  const reqId = ++loadSeq
  try {
    loading.value.page = true

    // 1) Load week anchored by the requested date
    let data = await getProductionReportWeek({ any_date_in_week: anyDate.value })
    if (reqId !== loadSeq) return

    // 2) UX fix: if backend asks to close a day that is outside of the shown week,
    // automatically switch the report to the week of close_date.
    // Typical case: Monday -> close_date is previous Friday (previous week).
    if (preferCloseWeek) {
      const closeDate = (data as any)?.close_hint?.close_date as string | undefined
      const dayDates = (data.days || []).map(d => String(d.date || ''))
      if (closeDate && dayDates.length && !dayDates.includes(closeDate)) {
        anyDate.value = closeDate
        data = await getProductionReportWeek({ any_date_in_week: closeDate })
        if (reqId !== loadSeq) return
      }
    }

    days.value = data.days || []
    closeHint.value = (data.close_hint as any) || null
    const closeDate = String(closeHint.value?.close_date || '')
    const availableDates = new Set((days.value || []).map(d => String(d.date || '')))
    if (!selectedCloseDate.value || !availableDates.has(selectedCloseDate.value)) {
      selectedCloseDate.value = closeDate
    }

    const raw = (data.rows || []) as any[]
    rows.value = raw.map((r) => ({ ...r }))
    projectFacts()
    pendingFacts.value = []
  } catch (e: any) {
    Notify.create({
      type: 'negative',
      message: 'Ошибка загрузки отчёта',
      caption: e?.message || String(e)
    })
  } finally {
    if (reqId === loadSeq) loading.value.page = false
  }
}

function upsertPending(item_id: number, d: string, fact_qty: number) {
  const key = `${item_id}|${d}`
  const idx = pendingFacts.value.findIndex(x => `${x.item_id}|${x.date}` === key)
  if (idx >= 0) {
    pendingFacts.value[idx] = { item_id, date: d, fact_qty }
  } else {
    pendingFacts.value.push({ item_id, date: d, fact_qty })
  }
}

function removePending(item_id: number, d: string) {
  pendingFacts.value = pendingFacts.value.filter(x => !(x.item_id === item_id && x.date === d))
}

function recalcWeekTotals(row: RowVM) {
  const ds = (days.value || []).map(d => d.date)
  let plan = 0
  let fact = 0
  for (const d of ds) {
    plan += getPlan(row, d)
    fact += Number(row[`fact_${d}`] ?? 0) || 0
  }
  row.plan_week = plan
  row.fact_week = fact
  row.remaining_week = plan - fact

  // keep map in sync for rendering
  row.fact_by_day = { ...(row.fact_by_day || {}) }
  for (const d of ds) row.fact_by_day[d] = Number(row[`fact_${d}`] ?? 0) || 0
}

function onFactInput(row: RowVM, d: string, val: any) {
  const qty = Math.max(0, Math.round(Number(val ?? row[`fact_${d}`] ?? 0) || 0))
  row[`fact_${d}`] = qty
  const initial = Number((row as any)?.__initialFacts?.[d] ?? 0) || 0
  if (qty === initial) {
    removePending(Number(row.item_id), d)
  } else {
    upsertPending(Number(row.item_id), d, qty)
  }
  recalcWeekTotals(row)
}

async function save() {
  if (!pendingFacts.value.length) {
    Notify.create({ type: 'info', message: 'Нет изменений' })
    return
  }
  try {
    loading.value.save = true
    const resp = await bulkUpsertProductionReportFact({
      entries: pendingFacts.value,
      rerun_editable_date: selectedCloseDate.value || undefined,
    })
    Notify.create({ type: 'positive', message: `Сохранено: ${resp.saved || 0}` })
    await load()
  } catch (e: any) {
    Notify.create({
      type: 'negative',
      message: 'Ошибка сохранения факта',
      caption: e?.message || String(e)
    })
  } finally {
    loading.value.save = false
  }
}

async function closeDay() {
  try {
    const dClose = selectedCloseDate.value || closeHint.value?.close_date
    if (!dClose) {
      Notify.create({ type: 'warning', message: 'Не выбрана дата закрытия' })
      return
    }
    const ok = window.confirm(`Закрыть день ${dClose || ''}? Перенос будет применён автоматически.`)
    if (!ok) return

    loading.value.close = true
    const resp = await closeProductionReportDay({ close_date: dClose })
    selectedCloseDate.value = String(resp?.close_date || dClose)
    Notify.create({
      type: 'positive',
      message: `День закрыт: ${resp.close_date} → ${resp.target_date}`
    })
    await load()
  } catch (e: any) {
    Notify.create({
      type: 'negative',
      message: 'Ошибка закрытия дня',
      caption: e?.message || String(e)
    })
  } finally {
    loading.value.close = false
  }
}

function confirmDiscardChanges(actionLabel: string): boolean {
  if (!pendingFacts.value.length) return true
  return window.confirm(`Есть несохраненные изменения (${pendingFacts.value.length}). ${actionLabel} и потерять их?`)
}

function onLoadClick() {
  if (!isValidISODate(anyDate.value)) {
    Notify.create({ type: 'warning', message: 'Некорректная дата. Используйте формат YYYY-MM-DD' })
    return
  }
  if (!confirmDiscardChanges('Перезагрузить данные')) return
  load(false)
}

function goWeek(deltaDays: number) {
  try {
    if (!isValidISODate(anyDate.value)) {
      Notify.create({ type: 'warning', message: 'Некорректная дата. Используйте формат YYYY-MM-DD' })
      return
    }
    if (!confirmDiscardChanges('Перейти на другую неделю')) return
    anyDate.value = dateShift(anyDate.value, deltaDays)
    load(false)
  } catch {
    // no-op
  }
}

const columns = computed(() => {
  const cols: any[] = [
    {
      name: 'item_name',
      label: 'Изделие',
      align: 'left',
      field: 'item_name',
      sortable: true,
      classes: 'sticky-name',
      headerClasses: 'sticky-name'
    }
  ]

  const ds = (days.value || []).map(d => d.date)
  for (const d of ds) {
    const wknd = isWeekend(d)
    const isClosed = closeStatusMap.value[d] === 'CLOSED'
    const headerClass = [wknd ? 'weekend-col' : '', isClosed ? 'closed-day-header' : ''].filter(Boolean).join(' ')
    const cellClass = [wknd ? 'weekend-cell' : '', isClosed ? 'closed-day-cell' : ''].filter(Boolean).join(' ')
    cols.push({
      name: `day_${d}`,
      label: dayLabel(d),
      align: 'center',
      field: (row: RowVM) => row[`fact_${d}`],
      sortable: false,
      classes: cellClass,
      headerClasses: headerClass,
      dateKey: d
    })
  }

  cols.push(
    {
      name: 'plan_week',
      label: 'План',
      align: 'right',
      field: 'plan_week',
      sortable: true
    },
    {
      name: 'fact_week',
      label: 'Факт',
      align: 'right',
      field: 'fact_week',
      sortable: true
    },
    {
      name: 'remaining_week',
      label: 'Остаток',
      align: 'right',
      field: 'remaining_week',
      sortable: true
    }
  )

  return cols
})

function cellClass(props: any): string {
  const col = props?.col
  if (!col?.name?.startsWith('day_')) return ''
  const d = col.dateKey
  if (closeStatusMap.value[d] === 'CLOSED') return 'closed-day-cell'
  return ''
}

onMounted(() => {
  load(true)
})
</script>

<style scoped>
.table-scroll {
  max-height: 72vh;
  overflow: auto;
  width: 100%;
  position: relative;
  scrollbar-gutter: stable both-edges;
}

.production-report-table :deep(th.sticky-name),
.production-report-table :deep(td.sticky-name) {
  position: sticky;
  left: 0;
  z-index: 20;
  background: #fff;
  box-shadow: 1px 0 0 rgba(0, 0, 0, 0.12);
}

.production-report-table :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 15;
  background: #eef3f9;
  color: #1f2a37;
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.02em;
  border-bottom: 1px solid #d4dde8;
}

.production-report-table :deep(thead th.sticky-name) {
  z-index: 25;
  background: #eef3f9;
}

.production-report-table :deep(th.weekend-col) {
  background: #f2f2f2;
}

.production-report-table :deep(td.weekend-cell) {
  background: #f8fbff;
}

.day-cell {
  min-width: 102px;
  line-height: 1.05;
}

.col-header {
  line-height: 1.1;
}

.fact-input {
  max-width: 70px;
  margin: 0 auto;
}

.closed-day-cell {
  background: #edf8ef;
}

.production-report-table :deep(th.closed-day-header) {
  background: #e4f1d4;
}

.wide-table {
  width: max-content;
  white-space: nowrap;
}

/* Ensure QTable doesn't create its own scroll container that breaks sticky */
.production-report-table :deep(.q-table__middle) { overflow: visible !important; }
.production-report-table :deep(.q-table__container) { overflow: visible; }

.production-report-table :deep(tbody td) {
  padding: 2px 6px;
  min-height: 22px;
  border-bottom: 1px solid #edf1f6;
  line-height: 1;
}
.production-report-table :deep(tbody tr:nth-child(even) td) {
  background: #fafcff;
}
.production-report-table :deep(tbody tr:hover td) {
  background: #eef6ff;
}
.production-report-table :deep(tbody td.weekend-cell) {
  background: #f8fbff;
}
.production-report-table :deep(tbody td.closed-day-cell) {
  background: #edf8ef;
}

.item-name-line {
  max-width: 360px;
}
.item-name-full {
  display: inline-block;
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.1;
}
.production-report-table :deep(.text-right) {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.production-report-table :deep(.day-cell .text-caption) {
  font-size: 10px;
  line-height: 1.05;
  margin-bottom: 1px;
}

.production-report-table :deep(.day-cell .q-field__control) {
  min-height: 18px;
  height: 18px;
  padding-left: 0;
  padding-right: 0;
  border: 0 !important;
  box-shadow: none !important;
  border-radius: 0;
  background: transparent;
}

.production-report-table :deep(.day-cell .q-field__native),
.production-report-table :deep(.day-cell input) {
  min-height: 18px;
  height: 18px;
  line-height: 1;
  font-size: 11px;
  padding: 0;
  text-align: center;
  background: transparent !important;
}

.production-report-table :deep(.day-cell input[type='number']) {
  -moz-appearance: textfield;
  appearance: textfield;
}

.production-report-table :deep(.day-cell input[type='number']::-webkit-outer-spin-button),
.production-report-table :deep(.day-cell input[type='number']::-webkit-inner-spin-button) {
  -webkit-appearance: none;
  margin: 0;
}

.load-btn-align {
  margin-bottom: 6px;
}
</style>

