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
        <div v-else>
          <q-badge
            v-for="(s, i) in (props.row.stages || [])"
            :key="i"
            color="primary"
            outline
            class="q-mr-xs q-mb-xs"
          >
            <!-- Этап: #id · дата · часы · участок -->
            {{ s.stage_id }} · {{ s.bucket_date || '—' }} · {{ fmt(s.hours) }} ч
            <span v-if="s.area_name"> · {{ s.area_name }}</span>
          </q-badge>
          <!-- Плашка по отсутствию норматива на любом этапе -->
          <q-badge
            v-if="(props.row.stages || []).some((s: any) => !!s?.missingNorm)"
            color="grey-8"
            text-color="white"
            outline
            class="q-ml-xs"
          >
            {{ t('mrp.badge.noNormPerUnit') }}
          </q-badge>
        </div>
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