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
                <q-btn outline icon="chevron_left" label="Пред. неделя" @click="goWeek(-7)" />
                <q-btn outline icon="chevron_right" label="След. неделя" @click="goWeek(7)" />
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
                />
              </div>
              <div class="col-auto">
                <q-btn color="primary" label="Загрузить" :loading="loading.page" @click="load" />
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
                    :disable="!pendingFacts.length"
                    @click="save"
                  />
                  <q-btn
                    color="negative"
                    outline
                    label="Закрыть день"
                    :loading="loading.close"
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
              >
                <template v-slot:header-cell="hprops">
                  <q-th :props="hprops" :class="hprops.col.headerClasses">
                    {{ hprops.col.label }}
                  </q-th>
                </template>

                <template v-slot:body-cell="props">
                  <q-td :props="props" :class="cellClass(props)">
                    <div v-if="props.col.name === 'item_name'">
                      <div class="text-weight-medium">{{ props.row.item_name }}</div>
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
                      <q-input
                        v-model.number="props.row[props.col.name]"
                        type="number"
                        dense
                        min="0"
                        step="1"
                        class="fact-input"
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

// Track pending changes (dedupe by item_id|date)
const pendingFacts = ref<Array<{ item_id: number; date: string; fact_qty: number }>>([])

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
    return `${wd} ${dm}`
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

const rerunEditableDate = computed<string | null>(() => {
  // Backend allows re-run corrections for D_close = previous_workday(today)
  // even if this day already has close_status=CLOSED.
  // UI should allow editing fact for this one day, so user can fix values and press "Закрыть день" again.
  return closeHint.value?.close_date || null
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

function isOverProduced(row: RowVM, dateStr: string): boolean {
  return getFact(row, dateStr) > getPlan(row, dateStr)
}

function projectFacts() {
  const ds = (days.value || []).map(d => d.date)
  rows.value = (rows.value || []).map((r) => {
    for (const d of ds) {
      const key = `fact_${d}`
      r[key] = Number(r.fact_by_day?.[d] ?? 0) || 0
    }
    return r
  })
}

async function load(preferCloseWeek: boolean = true) {
  try {
    loading.value.page = true

    // 1) Load week anchored by the requested date
    let data = await getProductionReportWeek({ any_date_in_week: anyDate.value })

    // 2) UX fix: if backend asks to close a day that is outside of the shown week,
    // automatically switch the report to the week of close_date.
    // Typical case: Monday -> close_date is previous Friday (previous week).
    if (preferCloseWeek) {
      const closeDate = (data as any)?.close_hint?.close_date as string | undefined
      const dayDates = (data.days || []).map(d => String(d.date || ''))
      if (closeDate && dayDates.length && !dayDates.includes(closeDate)) {
        anyDate.value = closeDate
        data = await getProductionReportWeek({ any_date_in_week: closeDate })
      }
    }

    days.value = data.days || []
    closeHint.value = (data.close_hint as any) || null

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
    loading.value.page = false
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
  const qty = Number(val ?? row[`fact_${d}`] ?? 0) || 0
  row[`fact_${d}`] = qty
  upsertPending(Number(row.item_id), d, qty)
  recalcWeekTotals(row)
}

async function save() {
  if (!pendingFacts.value.length) {
    Notify.create({ type: 'info', message: 'Нет изменений' })
    return
  }
  try {
    loading.value.save = true
    const resp = await bulkUpsertProductionReportFact({ entries: pendingFacts.value })
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
    const dClose = closeHint.value?.close_date
    const ok = window.confirm(`Закрыть день ${dClose || ''}? Перенос будет применён автоматически.`)
    if (!ok) return

    loading.value.close = true
    const resp = await closeProductionReportDay({})
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

// Quiet TS: avoid template access confusion with ref-object
const isLoadingPage = computed(() => loading.value.page)

function goWeek(deltaDays: number) {
  try {
    const d = new Date(anyDate.value)
    d.setDate(d.getDate() + deltaDays)
    anyDate.value = d.toISOString().slice(0, 10)
    load()
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
    const headerClass = wknd ? 'weekend-col' : ''
    const cellClass = wknd ? 'weekend-cell' : ''
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
  if (isDateClosed(d)) return 'closed-day-cell'
  return ''
}

onMounted(() => {
  load()
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
  background: #fff;
}

.production-report-table :deep(thead th.sticky-name) {
  z-index: 25;
}

.production-report-table :deep(th.weekend-col) {
  background: #f2f2f2;
}

.production-report-table :deep(td.weekend-cell) {
  background: #fafafa;
}

.day-cell {
  min-width: 110px;
}

.fact-input {
  max-width: 90px;
  margin: 0 auto;
}

.closed-day-cell {
  opacity: 0.85;
}

.wide-table {
  width: max-content;
  white-space: nowrap;
}

/* Ensure QTable doesn't create its own scroll container that breaks sticky */
.production-report-table :deep(.q-table__middle) { overflow: visible !important; }
.production-report-table :deep(.q-table__container) { overflow: visible; }
</style>

