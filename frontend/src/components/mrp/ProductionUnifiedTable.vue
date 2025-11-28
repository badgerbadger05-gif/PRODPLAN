<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="safeRows"
    :columns="columns"
    row-key="rowKey"
    :loading="loading"
    :pagination="{ rowsPerPage: 50 }"
    :no-data-label="t('mrp.table.noDataLabel')"
  >
    <template #body-cell-name="p">
      <q-td :props="p">
        <div>{{ p.row.item_name || t('mrp.placeholder.itemNameFallback', { id: p.row.item_id }) }}</div>
        <div class="q-mt-xs">
          <q-badge
            v-if="p.row.flags?.missingNorm"
            color="grey-8"
            text-color="white"
            class="q-mr-xs"
            outline
            size="sm"
          >
            {{ t('mrp.badge.noNormPerUnit') }}
          </q-badge>
          <q-badge
            v-if="p.row.flags?.missingArea"
            color="negative"
            text-color="white"
            outline
            size="sm"
          >
            {{ t('mrp.badge.noAreaForKind') }}
          </q-badge>
        </div>
      </q-td>
    </template>

    <template #body-cell-article="p">
      <q-td :props="p">{{ p.row.item_article || t('mrp.placeholder.noArticle') }}</q-td>
    </template>

    <template #body-cell-area="p">
      <q-td :props="p">
        {{ p.row.main_area_name || t('mrp.placeholder.noArticle') }}
      </q-td>
    </template>

    <template #body-cell-qty="p">
      <q-td :props="p" class="text-right">{{ fmtQty(p.row.qty, p.row.unit) }}</q-td>
    </template>

    <template #body-cell-norm_per_unit="p">
      <q-td :props="p" class="text-right">
        {{ fmt(resolveNormPerUnit(p.row.norm_hours_per_unit, p.row.norm_hours_total, p.row.qty)) }}
      </q-td>
    </template>

    <template #body-cell-norm_total="p">
      <q-td :props="p" class="text-right">{{ fmt(p.row.norm_hours_total) }}</q-td>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QTableColumn } from 'quasar'
import { useI18n } from 'vue-i18n'
import { useFormatting } from '../../composables/useFormatting'

type Flags = {
  missingArea?: boolean
  missingNorm?: boolean
  componentBlocked?: boolean
  componentPartial?: boolean
  capacityShiftDays?: number
}

type PlainProdRow = {
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  norm_hours_total: number
  norm_hours_per_unit?: number | null
  agg_key?: string
  rowKey: string
  main_area_id?: number | null
  main_area_name?: string | null
  flags?: Flags
}

const props = defineProps<{
  rows: PlainProdRow[]
  loading?: boolean
}>()

const { formatNumber: fmt, formatQty: fmtQty } = useFormatting()
const { t } = useI18n()

const columns = computed<QTableColumn<PlainProdRow>[]>(() => ([
  { name: 'name', label: t('mrp.columns.name'), field: (row: PlainProdRow) => row.item_name, align: 'left' },
  { name: 'article', label: t('mrp.columns.article'), field: (row: PlainProdRow) => row.item_article, align: 'left' },
  { name: 'area', label: t('mrp.columns.areaId'), field: (row: PlainProdRow) => row.main_area_name, align: 'left' },
  { name: 'qty', label: t('mrp.columns.qty'), field: (row: PlainProdRow) => row.qty, align: 'right' },
  { name: 'norm_per_unit', label: t('mrp.columns.normPerUnit'), field: (row: PlainProdRow) => row.norm_hours_per_unit, align: 'right' },
  { name: 'norm_total', label: t('mrp.columns.normTotal'), field: (row: PlainProdRow) => row.norm_hours_total, align: 'right' }
]))

const safeRows = computed(() => {
  const rowMap = new Map<string, PlainProdRow>()
  ;(props.rows || []).forEach(r => {
    const key = r.agg_key || `${r.item_id}|${r.unit || ''}`
    if (rowMap.has(key)) {
      const existingRow = rowMap.get(key)!
      rowMap.set(key, {
        ...existingRow,
        ...r,
        qty: (existingRow.qty || 0) + (r.qty || 0),
        norm_hours_total: (existingRow.norm_hours_total || 0) + (r.norm_hours_total || 0),
        // сохраняем участок и флаги, если появились
        main_area_id: r.main_area_id ?? existingRow.main_area_id,
        main_area_name: r.main_area_name ?? existingRow.main_area_name,
        flags: { ...(existingRow.flags || {}), ...(r.flags || {}) },
        rowKey: key
      })
    } else {
      rowMap.set(key, { ...r, rowKey: key })
    }
  })
  return Array.from(rowMap.values()).map(r => ({
    ...r,
    rowKey: r.agg_key || `${r.item_id}|${r.unit || ''}`
  }))
})

function resolveNormPerUnit(npu: number | null | undefined, nt: number | null | undefined, qty: number | null | undefined): number {
  if (npu != null) return Number(npu)
  const total = Number(nt ?? 0)
  const q = Number(qty ?? 0)
  return q > 0 ? total / q : 0
}

const loading = computed(() => !!props.loading)
</script>

<style scoped>
/* наследует компактные стили от страницы при необходимости */
</style>