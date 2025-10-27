<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="safeRows"
    :columns="columns"
    row-key="rowKey"
    :loading="loading"
    :pagination="{ rowsPerPage: 50 }"
  >
    <template #body-cell-name="p">
      <q-td :props="p">
        <div>{{ p.row.item_name || t('mrp.placeholder.itemNameFallback', { id: p.row.item_id }) }}</div>
      </q-td>
    </template>

    <template #body-cell-article="p">
      <q-td :props="p">{{ p.row.item_article || t('mrp.placeholder.noArticle') }}</q-td>
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
  { name: 'qty', label: t('mrp.columns.qty'), field: (row: PlainProdRow) => row.qty, align: 'right' },
  { name: 'norm_per_unit', label: t('mrp.columns.normPerUnit'), field: (row: PlainProdRow) => row.norm_hours_per_unit, align: 'right' },
  { name: 'norm_total', label: t('mrp.columns.normTotal'), field: (row: PlainProdRow) => row.norm_hours_total, align: 'right' }
]))

const safeRows = computed(() => {
  // Create a map to deduplicate rows based on agg_key or a combination of item_id and unit
  const rowMap = new Map<string, PlainProdRow>()
  
  ;(props.rows || []).forEach(r => {
    const key = r.agg_key || `${r.item_id}|${r.unit || ''}`
    // If row with same key already exists, merge/accumulate values as needed
    if (rowMap.has(key)) {
      const existingRow = rowMap.get(key)!
      // Accumulate quantities and norm hours when duplicates found
      rowMap.set(key, {
        ...r,
        qty: (existingRow.qty || 0) + (r.qty || 0),
        norm_hours_total: (existingRow.norm_hours_total || 0) + (r.norm_hours_total || 0),
        rowKey: key
      })
    } else {
      rowMap.set(key, {
        ...r,
        rowKey: key
      })
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