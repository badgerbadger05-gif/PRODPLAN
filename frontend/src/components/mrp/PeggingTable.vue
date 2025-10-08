<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="rows"
    :columns="columns"
    row-key="id"
    :loading="loading"
    :pagination="pagination"
    @request="$emit('request', $event)"
  >
    <template #body-cell-child_item_id="p">
      <q-td :props="p">
        <div>{{ p.row.child_item_id }}</div>
      </q-td>
    </template>

    <template #body-cell-parent_item_id="p">
      <q-td :props="p">
        <div>{{ p.row.parent_item_id }}</div>
      </q-td>
    </template>

    <template #body-cell-qty_contribution="p">
      <q-td :props="p" class="text-right">
        {{ fmt(p.row.qty_contribution) }}
      </q-td>
    </template>

    <template #body-cell-need_date="p">
      <q-td :props="p">
        {{ safeIsoDate(p.row.need_date) ?? '—' }}
      </q-td>
    </template>

    <template #body-cell-parent_need_date="p">
      <q-td :props="p">
        {{ safeIsoDate(p.row.parent_need_date) ?? '—' }}
      </q-td>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QTableColumn } from 'quasar'
import type { PeggingRow, PageState } from '../../types/mrp'
import { useFormatting } from '../../composables/useFormatting'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  rows: PeggingRow[]
  loading?: boolean
  pagination: PageState
}>()

defineEmits<{
  (e: 'request', payload: any): void
}>()

const { t } = useI18n()
const { formatNumber, safeIsoDate } = useFormatting()
const fmt = (v: number | string | null | undefined) => formatNumber(v, { fractionDigits: 3 })

const columns = computed<QTableColumn<PeggingRow>[]>(() => ([
  { name: 'child_item_id', label: t('mrp.pegging.child'), field: 'child_item_id', align: 'right' },
  { name: 'parent_item_id', label: t('mrp.pegging.parent'), field: 'parent_item_id', align: 'right' },
  { name: 'qty_contribution', label: t('mrp.pegging.qtyContribution'), field: 'qty_contribution', align: 'right' },
  { name: 'need_date', label: t('mrp.pegging.needDate'), field: 'need_date', align: 'left' },
  { name: 'parent_need_date', label: t('mrp.pegging.parentNeedDate'), field: 'parent_need_date', align: 'left' }
]))
</script>

<style scoped>
/* Компактные стили наследуются от страницы */
</style>