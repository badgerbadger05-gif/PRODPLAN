<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="rows"
    :columns="columns"
    row-key="key"
    :loading="loading"
    :pagination="pagination"
    @request="$emit('request', $event)"
    :no-data-label="t('mrp.table.noDataLabel')"
  />
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { QTableColumn } from 'quasar'
import type { CapacityRow } from '../../types/mrp'

type TablePagination = {
  page: number
  rowsPerPage: number
  rowsNumber: number
}

const props = defineProps<{
  rows: CapacityRow[]
  columns: QTableColumn<CapacityRow>[]
  loading?: boolean
  pagination: TablePagination
}>()

const { t } = useI18n()

defineEmits<{
  (e: 'request', payload: any): void
}>()
</script>

<style scoped>
/* Наследует компактные стили страницы */
</style>