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
      </q-td>
    </template>

    <template #body-cell-article="p">
      <q-td :props="p">{{ p.row.item_article || t('mrp.placeholder.noArticle') }}</q-td>
    </template>

    <template #body-cell-qty="p">
      <q-td :props="p" class="text-right">{{ fmtQty(p.row.qty, p.row.unit) }}</q-td>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QTableColumn } from 'quasar'
import { useI18n } from 'vue-i18n'
import { useFormatting } from '../../composables/useFormatting'

type PurchAggRow = {
  item_id: number
  item_name?: string | null
  item_article?: string | null
  unit?: string | null
  qty: number
  agg_key?: string
}

const props = defineProps<{
  rows: PurchAggRow[]
  loading?: boolean
}>()

const { formatQty: fmtQty } = useFormatting()
const { t } = useI18n()

const columns = computed<QTableColumn<PurchAggRow>[]>(() => ([
  { name: 'name', label: t('mrp.columns.name'), field: 'item_name', align: 'left' },
  { name: 'article', label: t('mrp.columns.article'), field: 'item_article', align: 'left' },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right' }
]))

const safeRows = computed(() => (props.rows || []).map(r => ({
  ...r,
  rowKey: r.agg_key || `${r.item_id}|${r.unit || ''}`
})))

const loading = computed(() => !!props.loading)
</script>

<style scoped>
/* компактные стили наследуются от страницы */
</style>