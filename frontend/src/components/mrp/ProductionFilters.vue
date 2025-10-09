<template>
  <div class="row items-center q-gutter-sm">
    <div v-if="title" class="text-subtitle2">{{ title }}</div>
    <q-space />

    <q-select
      v-model="local.bucket_type"
      :options="bucketOptions"
      emit-value
      map-options
      dense
      outlined
      :label="t('mrp.filters.bucket')"
      style="width: 150px"
    />

    <template v-if="showDayPicker">
      <q-input v-model="local.day_date" dense outlined :label="t('mrp.filters.dayDate')" style="width: 200px">
        <template #append>
          <q-btn dense flat round icon="event" @click.stop="showDayMenu = true" />
          <q-menu v-model="showDayMenu" anchor="bottom right" self="top right" cover>
            <q-date v-model="local.day_date" mask="YYYY-MM-DD" @update:model-value="onDayPicked" />
          </q-menu>
        </template>
      </q-input>

      <q-separator vertical class="q-mx-xs" />
    </template>

    <q-input v-model="local.date_from" dense outlined :label="t('mrp.filters.fromDate')" style="width: 200px" />
    <q-input v-model="local.date_to" dense outlined :label="t('mrp.filters.toDate')" style="width: 200px" />

    <q-btn
      :disable="applyDisabled"
      :loading="loading"
      dense
      color="primary"
      icon="search"
      @click="emitApply"
    />

    <q-btn
      :disable="loading"
      dense
      flat
      icon="refresh"
      @click="emitApply"
    />

    <q-btn
      :disable="loading"
      dense
      flat
      icon="clear"
      :label="t('mrp.filters.reset')"
      @click="emitReset"
    />

    <slot name="extra-actions" />
  </div>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ProductionFilters } from '../../types/mrp'

const props = defineProps<{
  modelValue: ProductionFilters
  loading?: boolean
  title?: string
  showDayPicker?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: ProductionFilters): void
  (e: 'apply'): void
  (e: 'reset'): void
  (e: 'day-picked', day: string): void
}>()

const { t } = useI18n()
const bucketOptions = computed(() => ([
  { label: t('mrp.filters.bucketOption.any'), value: undefined },
  { label: t('mrp.filters.bucketOption.daily'), value: 'daily' },
  { label: t('mrp.filters.bucketOption.weekly'), value: 'weekly' }
]))

const local = ref<ProductionFilters>({
  bucket_type: props.modelValue?.bucket_type,
  date_from: props.modelValue?.date_from,
  date_to: props.modelValue?.date_to,
  day_date: props.modelValue?.day_date
})

// Синхронизация с внешними изменениями модели
let isSyncingFromParent = false;
watch(() => props.modelValue, (v) => {
  if (v && !isSyncingFromParent) {
    isSyncingFromParent = true;
    local.value = { ...v };
    // Небольшая задержка для предотвращения конфликта с локальными изменениями
    setTimeout(() => { isSyncingFromParent = false; }, 0);
  }
}, { deep: true })

// Отправка изменений внешней модели
let isSyncingToParent = false;
watch(local, (v) => {
  if (!isSyncingToParent) {
    isSyncingToParent = true;
    emit('update:modelValue', { ...v });
    // Небольшая задержка для предотвращения конфликта с изменениями от родителя
    setTimeout(() => { isSyncingToParent = false; }, 0);
  }
}, { deep: true })

const showDayMenu = ref(false)

function onDayPicked(day: string) {
  // day уже в формате YYYY-MM-DD маской QDate
  local.value.day_date = (day || '').slice(0, 10)
  emit('day-picked', local.value.day_date || '')
  showDayMenu.value = false
}

const loading = computed(() => !!props.loading)
const applyDisabled = computed(() => loading.value)

function emitApply() {
  // Синхронно прокидываем актуальное состояние фильтров наверх перед применением,
  // чтобы родитель (MRPResultPage) видел обновлённый day_date при первом клике.
  emit('update:modelValue', { ...local.value })
  emit('apply')
}
function emitReset() {
  // Сбрасываем локальную модель и уведомляем родителя
  local.value = { bucket_type: undefined, date_from: undefined, date_to: undefined, day_date: undefined }
  emit('update:modelValue', { ...local.value })
  emit('reset')
}
</script>