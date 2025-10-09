<template>
  <q-table
    dense
    table-class="compact-rows"
    :rows="groups"
    :columns="columns"
    row-key="area_id"
    :loading="loading"
    :pagination="{ rowsPerPage: 50 }"
    hide-header
  >
    <template #body="props">
      <!-- Заголовок группы (дневная повестка по виду/участку) -->
      <q-tr :props="props" :key="`grp_day_${props.row.area_id}`">
        <q-td colspan="100%" class="bg-grey-2">
          <div class="text-subtitle1">
            <strong>{{ t('mrp.group.productionKind') }}</strong> {{ props.row.area_name }}
            <span class="text-grey q-ml-sm">
              · {{ t('mrp.agenda.positionsCountDay') }}: {{ (props.row.orders || []).length }}
              · {{ t('mrp.agenda.normDayHours') }}: {{ fmt(props.row.norm_sum_hours) }} ч
              · {{ t('mrp.agenda.qtyDay') }}: {{ fmtQty(props.row.sum_qty, 'шт') }}
            </span>
            <q-badge v-if="Number(props.row.cap_overload_hours || 0) > 0" class="q-ml-sm" color="negative" outline>
              {{ t('mrp.group.capOverloadHours') }}: {{ fmt(props.row.cap_overload_hours) }} ч
              <span v-if="props.row.cap_overload_percent != null"> ({{ fmt(Number(props.row.cap_overload_percent)) }}%)</span>
            </q-badge>
          </div>
        </q-td>
      </q-tr>

      <!-- Строки позиций на день -->
      <q-tr
        v-for="order in (props.row.orders || [])"
        :key="order.agg_key || `${order.item_id}|${order.unit || ''}`"
        :props="props"
        :class="{ 'text-negative': Boolean(order?.overload) }"
      >
        <q-td key="name" :props="props">
          <div>{{ order.item_name || t('mrp.placeholder.itemNameFallback', { id: order.item_id }) }}</div>
          <q-badge v-if="!(Number(order.norm_hours_per_unit || 0) > 0)" class="q-ml-xs" color="grey" outline>{{ t('mrp.badge.noNormPerUnit') }}</q-badge>
          <q-badge v-if="order.overload" class="q-ml-xs" color="negative" outline>{{ t('mrp.badge.overload') }}</q-badge>
        </q-td>
        <q-td key="article" :props="props">
          {{ order.item_article || t('mrp.placeholder.noArticle') }}
        </q-td>
        <q-td key="qty" :props="props" class="text-right">
          {{ fmtQty(order.display_qty != null ? order.display_qty : order.qty, order.unit) }}
        </q-td>
        <q-td key="norm_per_unit" :props="props" class="text-right">
          {{ fmt(resolveNormPerUnit(order.norm_hours_per_unit, order.norm_hours_total, order.qty)) }}
        </q-td>
        <q-td key="norm_total" :props="props" class="text-right">
          {{ fmt(order.display_norm_hours_total != null ? order.display_norm_hours_total : order.norm_hours_total) }}
        </q-td>
      </q-tr>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QTableColumn } from 'quasar'
import { useFormatting } from '../../composables/useFormatting'
import type { ProductionAgendaGroup } from '../../types/mrp'

const props = defineProps<{
  groups: ProductionAgendaGroup[]
  loading?: boolean
}>()

const { formatNumber: fmt, formatQty: fmtQty } = useFormatting()
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

const columns = computed<QTableColumn[]>(() => ([
  { name: 'name', label: t('mrp.columns.name'), field: 'item_name', align: 'left' },
  { name: 'article', label: t('mrp.columns.article'), field: 'item_article', align: 'left' },
  { name: 'qty', label: t('mrp.columns.qty'), field: 'qty', align: 'right' },
  { name: 'norm_per_unit', label: t('mrp.columns.normPerUnit'), field: 'norm_hours_per_unit', align: 'right' },
  { name: 'norm_total', label: t('mrp.columns.normTotal'), field: 'norm_hours_total', align: 'right' }
] as QTableColumn[]))

function resolveNormPerUnit(npu?: number | null, nt?: number | null, qty?: number | null) {
  if (npu != null) return Number(npu)
  const total = Number(nt ?? 0)
  const q = Number(qty ?? 0)
  return q > 0 ? total / q : 0
}
</script>

<style scoped>
/* Компактные стили наследуются от страницы */
</style>