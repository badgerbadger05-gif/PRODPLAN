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
      <!-- Заголовок группы (вид/участок) -->
      <q-tr :props="props" :key="`grp_${props.row.area_id}`">
        <q-td colspan="100%" class="bg-grey-2">
          <div class="text-subtitle1">
            <strong>{{ t('mrp.group.productionKind') }}</strong> {{ props.row.area_name }}
            <span class="text-grey q-ml-sm">
              · {{ t('mrp.group.ordersCount') }}: {{ (props.row.orders || []).length }}
              · {{ t('mrp.group.normSumHours') }}: {{ fmt(props.row.norm_sum_hours) }} ч
            </span>
            <q-badge v-if="props.row.min_days_to_need != null" class="q-ml-sm" color="orange" outline>
              {{ t('mrp.group.urgencyDays') }}: {{ props.row.min_days_to_need }} д
            </q-badge>
            <q-badge v-if="Number(props.row.cap_overload_hours || 0) > 0" class="q-ml-sm" color="negative" outline>
              {{ t('mrp.group.capOverloadHours') }}: {{ fmt(props.row.cap_overload_hours) }} ч
            </q-badge>
          </div>
        </q-td>
      </q-tr>

      <!-- Строки заказов внутри группы -->
      <q-tr
        v-for="order in deduplicateOrders(props.row.orders || [])"
        :key="order.agg_key || `${order.item_id}|${order.unit || ''}`"
        :props="props"
      >
        <q-td key="select" auto-width>
          <q-checkbox
            dense
            :model-value="isSelected(order)"
            @update:model-value="(val) => toggleOrder(order, Boolean(val))"
          />
        </q-td>
        <q-td key="name" :props="props">
          <div>{{ order.item_name || t('mrp.placeholder.itemNameFallback', { id: order.item_id }) }}</div>
          <q-badge
            v-if="order.badge"
            color="orange"
            text-color="black"
            outline
            size="sm"
            class="q-mt-xs"
          >
            {{ order.badge }}
          </q-badge>
        </q-td>
        <q-td key="article" :props="props">
          {{ order.item_article || t('mrp.placeholder.noArticle') }}
        </q-td>
        <q-td key="qty" :props="props" class="text-right">
          {{ fmtQty(order.qty, order.unit) }}
        </q-td>
        <q-td key="norm_per_unit" :props="props" class="text-right">
          {{ fmt(resolveNormPerUnit(order.norm_hours_per_unit, order.norm_hours_total, order.qty)) }}
        </q-td>
        <q-td key="norm_total" :props="props" class="text-right">
          {{ fmt(order.norm_hours_total) }}
        </q-td>
      </q-tr>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QTableColumn } from 'quasar'
import { useI18n } from 'vue-i18n'
import { useFormatting } from '../../composables/useFormatting'
import type { ProductionGroup } from '../../types/mrp'

const props = defineProps<{
  groups: ProductionGroup[]
  loading?: boolean
  selectedOrderIds?: number[]
}>()

const emit = defineEmits<{
  (e: 'update:selectedOrderIds', value: number[]): void
}>()

const { formatNumber: fmt, formatQty: fmtQty } = useFormatting()
const { t } = useI18n()

const columns = computed<QTableColumn[]>(() => ([
  { name: 'select', label: '', field: 'select', align: 'left' },
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

// Функция для удаления дубликатов заказов
function deduplicateOrders(orders: any[]) {
  const orderMap = new Map<string, any>();
  
  orders.forEach(order => {
    const key = order.agg_key || `${order.item_id}|${order.unit || ''}`;
    if (orderMap.has(key)) {
      const existingOrder = orderMap.get(key);
      // Объединяем значения qty и norm_hours_total при наличии дубликатов
      orderMap.set(key, {
        ...order,
        qty: (existingOrder.qty || 0) + (order.qty || 0),
        norm_hours_total: (existingOrder.norm_hours_total || 0) + (order.norm_hours_total || 0),
        badge: existingOrder.badge || order.badge || null,
        turning_blank_priority: Boolean(existingOrder.turning_blank_priority || order.turning_blank_priority),
        source_order_ids: [
          ...new Set([...(existingOrder.source_order_ids || []), ...(order.source_order_ids || [])].map(Number).filter(Boolean))
        ]
      });
    } else {
      orderMap.set(key, order);
    }
  });
  
  return Array.from(orderMap.values());
}

function sourceIds(order: any): number[] {
  const ids = Array.isArray(order?.source_order_ids) ? order.source_order_ids : []
  return ids.map(Number).filter((id) => Number.isFinite(id) && id > 0)
}

function isSelected(order: any): boolean {
  const selected = new Set((props.selectedOrderIds || []).map(Number))
  const ids = sourceIds(order)
  return ids.length > 0 && ids.every((id) => selected.has(id))
}

function toggleOrder(order: any, checked: boolean) {
  const selected = new Set((props.selectedOrderIds || []).map(Number))
  for (const id of sourceIds(order)) {
    if (checked) selected.add(id)
    else selected.delete(id)
  }
  emit('update:selectedOrderIds', Array.from(selected.values()).sort((a, b) => a - b))
}
</script>

<style scoped>
/* Компактные стили наследуются от страницы */
</style>
