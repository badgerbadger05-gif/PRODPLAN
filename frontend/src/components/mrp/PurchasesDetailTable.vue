<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="rows"
    :columns="columns"
    row-key="purchase_id"
    :loading="loading"
    :pagination="pagination"
    @request="$emit('request', $event)"
  />
</template>

<script setup lang="ts">
import type { QTableColumn } from 'quasar'
import type { PurchaseRow } from '../../types/mrp'

type TablePagination = {
  page: number
  rowsPerPage: number
  rowsNumber: number
}

const props = defineProps<{
  rows: PurchaseRow[]
  columns: QTableColumn<PurchaseRow>[]
  loading?: boolean
  pagination: TablePagination
}>()

defineEmits<{
  (e: 'request', payload: any): void
}>()
</script>

<style scoped>
/* Наследует компактные стили страницы */
</style>