<template>
  <q-card flat bordered>
    <q-card-section>
      <div class="row items-center q-gutter-sm">
        <div class="text-subtitle2">{{ t('mrp.title', { runId }) }}</div>
        <q-space />
        <q-chip
          v-if="summary?.run?.status"
          :color="statusColor(summary?.run?.status)"
          text-color="white"
          size="sm"
        >
          {{ summary?.run?.status }}
        </q-chip>
      </div>
    </q-card-section>

    <q-separator />

    <q-card-section>
      <div class="row q-col-gutter-md">
        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.run') }}</div>
          <div class="text-body1">{{ runId }}</div>
        </div>

        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.startedAt') }}</div>
          <div class="text-body1">{{ summary?.run?.started_at || '—' }}</div>
        </div>

        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.finishedAt') }}</div>
          <div class="text-body1">{{ summary?.run?.finished_at || '—' }}</div>
        </div>

        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.horizonDays') }}</div>
          <div class="text-body1">{{ summary?.run?.horizon_days ?? '—' }}</div>
        </div>
      </div>

      <div class="row q-col-gutter-md q-mt-sm">
        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.summary.productionOrders') }}</div>
          <div class="text-body1">{{ summary?.counts?.production_orders ?? 0 }}</div>
        </div>

        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.summary.purchaseRequests') }}</div>
          <div class="text-body1">{{ summary?.counts?.purchase_requests ?? 0 }}</div>
        </div>

        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.summary.overloadedBuckets') }}</div>
          <div class="text-body1">{{ summary?.capacity?.overloaded_buckets ?? 0 }}</div>
        </div>

        <div class="col-6 col-md-3">
          <div class="text-caption text-grey">{{ t('mrp.summary.overloadTotal') }}</div>
          <div class="text-body1">{{ fmt(summary?.capacity?.overload_total) }}</div>
        </div>
      </div>
    </q-card-section>

    <template v-if="(summary?.warnings || []).length > 0">
      <q-separator />
      <q-card-section>
        <q-expansion-item
          icon="warning"
          :label="t('mrp.summary.warnings.title')"
          :caption="t('mrp.summary.warnings.caption')"
          dense
          switch-toggle-side
        >
          <div class="row q-col-gutter-xs q-pt-sm">
            <q-chip
              v-for="(w, idx) in (summary?.warnings || [])"
              :key="idx"
              color="orange"
              text-color="black"
              outline
              size="sm"
            >
              {{ warnText(w) }}
            </q-chip>
          </div>
        </q-expansion-item>
      </q-card-section>
    </template>

    <template v-if="kindIssuesCount > 0">
      <q-separator />
      <q-card-section class="row items-center">
        <q-btn
          dense
          color="negative"
          icon="report_problem"
          :label="t('mrp.summary.kindIssues.button')"
          @click="$emit('open-kind-issues')"
        />
        <span class="text-grey q-ml-sm">({{ kindIssuesCount }})</span>
      </q-card-section>
    </template>

    <template v-if="summaryIndicatorsVisible">
      <q-separator />
      <q-card-section>
        <div class="row q-col-gutter-sm">
          <q-chip
            v-if="missingNormsCount > 0"
            color="grey-8"
            text-color="white"
            icon="schedule"
            size="sm"
          >
            {{ t('mrp.summary.missingNorms') }}: {{ missingNormsCount }}
          </q-chip>

          <q-chip
            v-if="componentBlockedCount > 0"
            color="negative"
            text-color="white"
            icon="block"
            size="sm"
          >
            {{ t('mrp.summary.componentShortagesBlocked') }}: {{ componentBlockedCount }}
          </q-chip>

          <q-chip
            v-if="componentPartialCount > 0"
            color="warning"
            text-color="black"
            icon="inventory_2"
            size="sm"
          >
            {{ t('mrp.summary.componentShortagesPartial') }}: {{ componentPartialCount }}
          </q-chip>
        </div>
      </q-card-section>
    </template>
  </q-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormatting } from '../../composables/useFormatting'
import type { MRPSummary } from '../../types/mrp'

const props = defineProps<{
  runId: number
  summary: MRPSummary | null
}>()

const emit = defineEmits<{
  (e: 'open-kind-issues'): void
}>()

const { t } = useI18n()
const { statusColor, warnText, formatNumber } = useFormatting()
const fmt = (v: any) => formatNumber(v, { fractionDigits: 3 })

// Подсчёт проблем сопоставления вида производства участкам
const kindIssuesCount = computed(() => {
  // Новый приоритет: структурированное поле summary.kindIssues.total
  const structured = Number((props.summary as any)?.kindIssues?.total ?? 0)
  if (structured > 0) return structured
  // Фолбэк по кодам из warnings
  const arr = (props.summary?.warnings || []) as any[]
  return arr.filter((w: any) => {
    const code = String(w?.code || '')
    return code === 'NO_AREA_FOR_PRODUCTION_KIND' || code === 'NO_PRODUCTION_KIND'
  }).length
})

// Индикаторы по новым агрегатам
const missingNormsCount = computed(() => Number((props.summary as any)?.missingNorms?.total ?? 0))
const componentBlockedCount = computed(() => Number((props.summary as any)?.componentShortages?.blocked ?? 0))
const componentPartialCount = computed(() => Number((props.summary as any)?.componentShortages?.partial ?? 0))
const summaryIndicatorsVisible = computed(
  () => (missingNormsCount.value + componentBlockedCount.value + componentPartialCount.value) > 0
)
</script>

<style scoped>
.text-body1 {
  font-weight: 500;
}
</style>