<template>
  <q-page class="q-pa-lg">
    <div class="row items-center q-mb-md">
      <div class="col-auto">
        <q-btn flat round icon="arrow_back" @click="onBack" />
      </div>
      <div class="col">
        <div class="text-h5">Спецификация (дерево)</div>
        <div class="text-caption text-grey-7">Иерархическое представление состава изделия и операций</div>
      </div>
    </div>

    <q-card class="q-mb-md">
      <q-card-section>
        <div class="row q-col-gutter-md items-end">
          <div class="col-12 col-md-4">
            <q-input
              v-model="itemCode"
              label="Артикул/Код изделия (item_code)"
              dense
              clearable
            />
          </div>
          <div class="col-6 col-md-2">
            <q-input
              v-model.number="rootQty"
              type="number"
              label="Количество корня"
              dense
              :min="0.001"
              :step="1"
            />
          </div>
          <div class="col-auto">
            <q-btn
              color="primary"
              label="Загрузить"
              :disable="!itemCode"
              :loading="loading.root"
              @click="loadRoot"
            />
          </div>
          <div class="col-auto">
            <q-btn
              color="primary"
              outline
              label="Развернуть полностью"
              :disable="!itemCode"
              :loading="loading.full"
              @click="loadFull"
            />
          </div>
        </div>
      </q-card-section>
    </q-card>

    <q-card>
      <q-card-section>
        <!-- Заголовок "таблицы" поверх дерева -->
        <div class="row q-pa-sm bg-grey-2 text-weight-bold">
          <div class="col-4">Наименование</div>
          <div class="col-2 text-right">Артикул</div>
          <div class="col-2 text-right">Этап</div>
          <div class="col-2 text-right">Метод пополнения</div>
          <div class="col-1 text-right">Кол-во</div>
          <div class="col-1 text-right">Ед./Норма</div>
        </div>

        <!-- Дерево спецификации -->
        <q-tree
          :nodes="displayRows"
          node-key="id"
          v-model:expanded="expanded"
          no-connectors
        >
          <template #default-header="props">
            <div class="row full-width items-center q-pa-xs">
              <div class="col-4 row items-center no-wrap">
                <q-icon
                  v-if="props.node.type === 'operation'"
                  name="construction"
                  size="16px"
                  class="q-mr-sm text-grey-7"
                />
                <q-icon
                  v-else
                  name="inventory_2"
                  size="16px"
                  class="q-mr-sm text-grey-7"
                />
                <span>
                  {{ props.node.type === 'operation'
                    ? ((props.node.operation && props.node.operation.name) || '—')
                    : (props.node.name || '—') }}
                </span>
                <q-spinner-dots
                  v-if="props.node.__loading__"
                  size="sm"
                  color="primary"
                  class="q-ml-sm"
                />
              </div>
              <div class="col-2 text-right">{{ props.node.article || '' }}</div>
              <div class="col-2 text-right">{{ props.node.stage?.name || '' }}</div>
              <div class="col-2 text-right">
                <template v-if="props.node.type === 'operation'"></template>
                <template v-else>
                  {{ props.node.replenishmentMethod || '' }}
                </template>
              </div>
              <div class="col-1 text-right">{{ props.node.qtyPerParent ?? '' }}</div>
              <div class="col-1 text-right">
                <template v-if="props.node.type === 'operation'">
                  {{ props.node.timeNormNh != null ? (props.node.timeNormNh + ' н/ч') : '' }}
                </template>
                <template v-else>
                  {{ props.node.unit || '' }}
                </template>
              </div>
            </div>
          </template>
        </q-tree>
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Notify } from 'quasar'
import api, {
  getSpecificationFull,
  type SpecNode
} from '../services/api'
// Route/query
const route = useRoute()
const router = useRouter()
function onBack() { try { router.back() } catch {} }

const itemCode = ref<string | null>(null)
const rootQty = ref<number>(1)

// Table data
const rows = ref<SpecNode[]>([])
const expanded = ref<string[]>([])
const pagination = ref({ rowsPerPage: 0 }) // disable internal pagination
const loading = ref({ root: false, full: false, table: false })

const rootNode = computed<SpecNode | null>(() => (rows.value.length > 0 ? (rows.value[0] as SpecNode) : null))

const columns = computed(() => {
  const cols: any[] = [
    { name: 'name', label: 'Наименование', field: 'name', align: 'left' as const, sortable: false },
    { name: 'article', label: 'Артикул', field: 'article', align: 'left' as const, sortable: false },
    {
      name: 'stage', label: 'Этап', align: 'left' as const, sortable: false,
      field: (row: any) => row.stage?.name || null
    },
    {
      name: 'operation', label: 'Операция', align: 'left' as const, sortable: false,
      field: (row: any) => row.operation?.name || null
    },
    {
      name: 'qtyPerParent', label: 'Кол-во (в род.)', align: 'right' as const, sortable: false,
      field: 'qtyPerParent',
      format: (v: any) => v ?? ''
    },
    { name: 'unit', label: 'Ед.', field: 'unit', align: 'left' as const, sortable: false },
    {
      name: 'timeNormNh', label: 'Норма, н/ч', field: 'timeNormNh', align: 'right' as const, sortable: false,
      format: (v: any) => v ?? ''
    },
    {
      name: 'treeQty', label: 'Σ Кол-во', align: 'right' as const, sortable: false,
      field: (row: any) => row?.computed?.treeQty ?? null,
      format: (v: any) => v ?? ''
    },
    {
      name: 'treeTimeNh', label: 'Σ Время, н/ч', align: 'right' as const, sortable: false,
      field: (row: any) => row?.computed?.treeTimeNh ?? null,
      format: (v: any) => v ?? ''
    },
    { name: 'warnings', label: 'Проблемы', align: 'left' as const }
  ]
  return cols
})

const displayRows = computed(() => rows.value)

// Генерация уникальных id на уровне фронта (подстраховка для QTable tree)
function ensureUniqueIds(nodes: SpecNode[], parentPath = ''): SpecNode[] {
  return nodes.map((node, index) => {
    const uniqueId = parentPath ? `${parentPath}.${index}` : `${index}`
    const processedNode: SpecNode = {
      ...node,
      id: String(node.id || uniqueId),
      children: Array.isArray(node.children) ? ensureUniqueIds(node.children, uniqueId) : []
    }
    return processedNode
  })
}

// Валидация структуры узла
function validateNodeStructure(node: any): boolean {
  return !!(node && typeof node.id === 'string' && node.id.length > 0 && (node.children === undefined || Array.isArray(node.children)))
}

function warningLabel(code: string): string {
  switch (code) {
    case 'NO_STAGE': return 'Нет этапа'
    case 'NO_TIME_NORM': return 'Нет нормы времени'
    case 'DUPLICATE': return 'Дубликат'
    case 'CYCLE_DETECTED': return 'Цикл BOM'
    default: return code
  }
}

async function loadRoot() {
  if (!itemCode.value) return
  try {
    loading.value.root = true
    loading.value.table = true
    rows.value = []
    expanded.value = []
    console.log('[SpecPage] loadRoot (full): request', {
      item_code: itemCode.value,
      root_qty: Number(rootQty.value) || 1,
      max_depth: 15
    })
    const { nodes, meta } = await getSpecificationFull({
      item_code: itemCode.value!,
      root_qty: Number(rootQty.value) || 1,
      max_depth: 15
    })
    console.log('[SpecPage] loadRoot (full): response', {
      nodeCount: Array.isArray(nodes) ? nodes.length : 0,
      meta
    })
    const root = (nodes && nodes[0]) ? nodes[0] : null
    if (!root || !validateNodeStructure(root)) {
      console.warn('[SpecPage] loadRoot: invalid or empty root node', root)
      Notify.create({ type: 'warning', message: 'Некорректная структура данных корневого узла' })
      return
    }
    const processed = ensureUniqueIds([root], String(root.id || 'root'))
    rows.value = processed
    // Развернуть все item-узлы
    expanded.value = collectAllItemIds(rows.value)
    console.log('[SpecPage] loadRoot: expanded count=', expanded.value.length)
  } catch (e: any) {
    const msg = e?.response?.data?.detail || 'Ошибка загрузки корня спецификации'
    Notify.create({ type: 'negative', message: msg })
  } finally {
    loading.value.root = false
    loading.value.table = false
  }
}



function collectAllItemIds(nodes: SpecNode[] | undefined): string[] {
  const acc: string[] = []
  function walk(list: SpecNode[] | undefined) {
    if (!list) return
    for (const n of list) {
      if (n && typeof n.id === 'string') {
        if (n.type === 'item') acc.push(n.id)
      }
      if (Array.isArray(n.children) && n.children.length) {
        walk(n.children)
      }
    }
  }
  walk(nodes)
  return acc
}

async function loadFull() {
  if (!itemCode.value) return
  try {
    loading.value.full = true
    rows.value = []
    expanded.value = []
    console.log('[SpecPage] loadFull: request', {
      item_code: itemCode.value,
      root_qty: Number(rootQty.value) || 1,
      max_depth: 15
    })
    const { nodes, meta } = await getSpecificationFull({
      item_code: itemCode.value!,
      root_qty: Number(rootQty.value) || 1,
      max_depth: 15
    })
    console.log('[SpecPage] loadFull: response', {
      nodeCount: Array.isArray(nodes) ? nodes.length : 0,
      meta
    })
    const root = (nodes && nodes[0]) ? nodes[0] : null
    if (!root || !validateNodeStructure(root)) {
      console.warn('[SpecPage] loadFull: invalid or empty root node', root)
      Notify.create({ type: 'warning', message: 'Некорректная структура данных корневого узла' })
      return
    }
    // Убедимся, что ids уникальны по дереву
    const processed = ensureUniqueIds([root], String(root.id || 'root'))
    rows.value = processed
    // Развернуть все уровни (только item-узлы)
    expanded.value = collectAllItemIds(rows.value)
    console.log('[SpecPage] loadFull: expanded count=', expanded.value.length)
  } catch (e: any) {
    const msg = e?.response?.data?.detail || 'Ошибка загрузки полной спецификации'
    console.error('[SpecPage] loadFull: error', e?.response?.data || e)
    Notify.create({ type: 'negative', message: msg })
  } finally {
    loading.value.full = false
  }
}

// Watch expanded changes for lazy-load (track only newly expanded)


onMounted(() => {
  // Init from query
  const qItemCode = String(route.query.item_code || '').trim()
  const qQty = Number(route.query.qty || 1)
  if (qItemCode) {
    itemCode.value = qItemCode
    rootQty.value = (qQty > 0 ? qQty : 1)
    loadRoot()
  }
})
</script>

<style scoped>
/* Optional styling tweaks */
</style>