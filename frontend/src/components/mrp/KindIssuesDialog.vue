<template>
  <q-dialog v-model="visible">
    <q-card style="min-width: 900px; max-width: 95vw;">
      <q-card-section class="row items-center">
        <div class="text-h6">{{ t('mrp.kindIssues.title') }}</div>
        <q-space />
        <q-btn flat icon="close" round dense v-close-popup />
      </q-card-section>
      <q-separator />
      <q-card-section>
        <q-table
          dense
          :rows="normalizedRows"
          :columns="columns"
          row-key="key"
          :pagination="{ rowsPerPage: 50 }"
        />
      </q-card-section>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QTableColumn } from 'quasar'
import { useI18n } from 'vue-i18n'

type KindIssue = {
  code: string
  msg?: string
  context?: Record<string, unknown>
  production_kind_id?: number | null
  production_kind_name?: string | null
  item_id?: number | null
  item_name?: string | null
  item_article?: string | null
  root_item_article?: string | null
  spec_id?: number | string | null
  spec_name?: string | null
  spec_code?: string | null
  spec_ref1c?: string | null
}

const props = defineProps<{
  modelValue: boolean
  issues: KindIssue[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v)
})

const { t } = useI18n()

type RowT = KindIssue & { key: number }
const columns = computed<QTableColumn<RowT>[]>(() => ([
  { name: 'pk', label: t('mrp.kindIssues.columns.kindId'), field: (r: RowT) => r.production_kind_id, align: 'right' },
  { name: 'pk_name', label: t('mrp.kindIssues.columns.kindName'), field: (r: RowT) => r.production_kind_name, align: 'left' },
  { name: 'item', label: t('mrp.kindIssues.columns.item'), field: (r: RowT) => r.item_name || (r.item_id ? t('mrp.placeholder.itemNameFallback', { id: r.item_id }) : t('mrp.placeholder.noArticle')), align: 'left' },
  { name: 'article', label: t('mrp.kindIssues.columns.article'), field: (r: RowT) => r.item_article, align: 'left' },
  { name: 'root_article', label: t('mrp.kindIssues.columns.rootArticle'), field: (r: RowT) => r.root_item_article, align: 'left' },
  { name: 'spec', label: t('mrp.kindIssues.columns.spec'), field: (r: RowT) => r.spec_name || r.spec_code || r.spec_ref1c || r.spec_id || t('mrp.placeholder.noArticle'), align: 'left' },
  { name: 'code', label: t('mrp.kindIssues.columns.code'), field: (r: RowT) => r.code, align: 'left' }
]))

const normalizedRows = computed<RowT[]>(() => {
  const list = Array.isArray(props.issues) ? props.issues : []
  return list.map((w, idx) => ({
    key: idx,
    ...w
  }))
})
</script>

<style scoped>
/* Компактные стили наследуются от страницы */
</style>