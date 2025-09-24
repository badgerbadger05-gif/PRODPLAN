<template>
 <q-table
    title="Номенклатура"
    :rows="items"
    :columns="columns"
    row-key="item_id"
    :loading="loading"
    :pagination="initialPagination"
  >
    <template v-slot:top>
      <div class="text-h6">Номенклатура</div>
      <q-space />
      <q-btn color="primary" label="Добавить" @click="addItem" />
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Item } from '../types/item'
import api from '../services/api'

const items = ref<Item[]>([])
const loading = ref(false)

const columns = [
  {
    name: 'item_code',
    required: true,
    label: 'Код',
    align: 'left',
    field: 'item_code',
    sortable: true
  },
  {
    name: 'item_name',
    required: true,
    label: 'Наименование',
    align: 'left',
    field: 'item_name',
    sortable: true
  },
  {
    name: 'item_article',
    label: 'Артикул',
    align: 'left',
    field: 'item_article',
    sortable: true
  },
  {
    name: 'stock_qty',
    label: 'Остаток',
    align: 'right',
    field: 'stock_qty',
    sortable: true
  }
]

const initialPagination = ref({
  sortBy: 'item_name',
  descending: false,
 page: 1,
  rowsPerPage: 10
})

const fetchItems = async () => {
  loading.value = true
  try {
    const response = await api.get<Item[]>('/items')
    items.value = response.data
  } catch (error) {
    console.error('Ошибка при загрузке номенклатуры:', error)
  } finally {
    loading.value = false
  }
}

const addItem = () => {
  // Логика добавления нового элемента
  console.log('Добавить новый элемент')
}

onMounted(() => {
  fetchItems()
})
</script>