<template>
  <div class="row items-center q-gutter-sm">
    <div v-if="title" class="text-subtitle2">{{ title }}</div>
    <q-space />


    <q-input v-model="local.date_from" dense outlined :label="t('mrp.filters.fromDate')" style="width: 200px">
      <template #append>
        <q-icon name="event" class="cursor-pointer">
          <q-popup-proxy cover transition-show="scale" transition-hide="scale">
            <q-date v-model="local.date_from" mask="YYYY-MM-DD" :locale="ruDateLocale">
              <div class="row items-center justify-end">
                <q-btn v-close-popup label="Закрыть" color="primary" flat />
              </div>
            </q-date>
          </q-popup-proxy>
        </q-icon>
      </template>
    </q-input>
    
    <q-input v-model="local.date_to" dense outlined :label="t('mrp.filters.toDate')" style="width: 200px">
      <template #append>
        <q-icon name="event" class="cursor-pointer">
          <q-popup-proxy cover transition-show="scale" transition-hide="scale">
            <q-date v-model="local.date_to" mask="YYYY-MM-DD" :locale="ruDateLocale">
              <div class="row items-center justify-end">
                <q-btn v-close-popup label="Закрыть" color="primary" flat />
              </div>
            </q-date>
          </q-popup-proxy>
        </q-icon>
      </template>
    </q-input>


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

// Локализация для календаря
const ruDateLocale = {
  days: 'Воскресенье_Понедельник_Вторник_Среда_Четверг_Пятница_Суббота'.split('_'),
  daysShort: 'Вс_Пн_Вт_Ср_Чт_Пт_Сб'.split('_'),
  months: 'Январь_Февраль_Март_Апрель_Май_Июнь_Июль_Август_Сентябрь_Октябрь_Ноябрь_Декабрь'.split('_'),
 monthsShort: 'Янв_Фев_Мар_Апр_Май_Июн_Июл_Авг_Сен_Окт_Ноя_Дек'.split('_'),
  firstDayOfWeek: 1, // Понедельник как первый день недели
  format24h: true
}

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
  date_from: props.modelValue?.date_from,
  date_to: props.modelValue?.date_to
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

// Удаляем переменную showDayMenu и функцию onDayPicked, так как поле day_date больше не используется

const loading = computed(() => !!props.loading)
const applyDisabled = computed(() => loading.value)

function emitApply() {
  emit('apply')
}
function emitReset() {
  // Сбрасываем локальную модель и уведомляем родителя
  local.value = { date_from: undefined, date_to: undefined }
  emit('update:modelValue', { ...local.value })
  emit('reset')
}
</script>