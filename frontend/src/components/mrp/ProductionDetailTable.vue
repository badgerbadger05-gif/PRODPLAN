<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="rows"
    :columns="columns"
    row-key="order_id"
    :loading="loading"
    :pagination="pagination"
    @request="$emit('request', $event)"
  >
    <template #body-cell-stages="props">
      <q-td :props="props">
        <div v-if="(props.row.stages || []).length === 0" class="text-grey">{{ t('mrp.placeholder.noArticle') }}</div>
        <q-badge
          v-for="(s, i) in (props.row.stages || [])"
          :key="i"
          color="primary"
          outline
          class="q-mr-xs q-mb-xs"
        >
          {{ s.stage_id }} · {{ s.bucket_date }} · {{ fmt(s.hours) }} ч
        </q-badge>
      </q-td>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import type { ProductionOrder } from '../../types/mrp'
import { useI18n } from 'vue-i18n'
import { useFormatting } from '../../composables/useFormatting'

type TablePagination = {
  page: number
  rowsPerPage: number
  rowsNumber: number
}

const props = defineProps<{
  rows: ProductionOrder[]
  columns: QTableColumn<ProductionOrder>[]
  loading?: boolean
  pagination: TablePagination
}>()

defineEmits<{
  (e: 'request', payload: any): void
}>()

const { t } = useI18n()
const { formatNumber } = useFormatting()
const fmt = (v: any) => formatNumber(v, { fractionDigits: 3 })
</script>

<style scoped>
/* Наследует компактные стили страницы */
</style>