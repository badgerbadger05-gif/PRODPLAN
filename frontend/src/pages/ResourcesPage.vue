<template>
  <div class="q-pa-md">
    <div class="row items-center q-mb-md">
      <h4 class="text-h4 q-ma-none">Производственные ресурсы</h4>
      <q-space />
      <q-btn 
        label="Добавить участок" 
        color="primary" 
        icon="add" 
        @click="showCreateDialog = true"
      />
    </div>

    <q-card class="q-mb-md">
      <q-card-section>
        <q-input 
          v-model="searchTerm" 
          outlined 
          dense 
          label="Поиск участков..." 
          clearable 
          class="q-mb-md"
        />
      </q-card-section>
    </q-card>

    <div class="row q-gutter-md">
      <div 
        v-for="resource in filteredResources" 
        :key="resource.resource_id" 
        class="col-xs-12 col-md-6 col-lg-4"
      >
        <q-card class="resource-card">
          <q-card-section class="bg-primary text-white">
            <div class="row items-center no-wrap">
              <div class="col">
                <div class="text-h6">{{ resource.resource_name }}</div>
              </div>
              <div class="col-auto">
                <q-btn 
                  dense 
                  flat 
                  icon="more_vert" 
                  color="white"
                >
                  <q-menu>
                    <q-item clickable v-close-popup @click="editResource(resource)">
                      <q-item-section>
                        <q-item-label>Редактировать</q-item-label>
                      </q-item-section>
                    </q-item>
                    <q-item clickable v-close-popup @click="deleteResource(resource.resource_id)">
                      <q-item-section>
                        <q-item-label>Удалить</q-item-label>
                      </q-item-section>
                    </q-item>
                  </q-menu>
                </q-btn>
              </div>
            </div>
          </q-card-section>

          <q-card-section>
            <div class="row q-col-gutter-md">
              <div class="col-12">
                <q-input
                  v-model.number="resource.shift_offset"
                  label="Сдвиг планирования"
                  type="number"
                  outlined
                  dense
                  @blur="updateResource(resource)"
                />
              </div>
              <div class="col-12">
                <q-input
                  v-model.number="resource.planning_range"
                  label="Диапазон планирования (дни)"
                  type="number"
                  outlined
                  dense
                  @blur="updateResource(resource)"
                />
              </div>
              <div class="col-12">
                <q-input
                  v-model.number="resource.capacity"
                  label="Мощность"
                  type="number"
                  outlined
                  dense
                  @blur="updateResource(resource)"
                />
              </div>
              <div class="col-12">
                <q-select
                  v-model="resource.work_schedule"
                  :options="workScheduleOptions"
                  label="График работы"
                  outlined
                  dense
                  @update:model-value="updateResource(resource)"
                />
              </div>
              <div class="col-12">
                <q-input
                  v-model.number="resource.daily_work_hours"
                  label="Рабочее время в сутки (часы)"
                  type="number"
                  outlined
                  dense
                  @blur="updateResource(resource)"
                />
              </div>
              <div class="col-12">
                <q-input
                  v-model.number="resource.buffer_days"
                  label="Буфер (дней)"
                  type="number"
                  outlined
                  dense
                  :min="0"
                  :step="1"
                  @blur="updateResource(resource)"
                />
              </div>
            </div>
          </q-card-section>

          <q-card-section>
            <div class="text-subtitle2 q-mb-sm">Виды производства:</div>
            <q-chip
              v-for="pk in getResourceProductionKinds(resource.resource_id)"
              :key="pk.id"
              removable
              @remove="removeProductionKindFromResource(resource.resource_id, pk.production_kind_id)"
            >
              {{ pk.production_kind_name || ('ID ' + pk.production_kind_id) }}
            </q-chip>

            <q-select
              v-model="selectedKindByResource[resource.resource_id]"
              :options="availableKinds(resource.resource_id)"
              option-label="name"
              option-value="id"
              emit-value
              map-options
              label="Добавить вид производства"
              outlined
              dense
              class="q-mt-md"
            >
              <template v-slot:after>
                <q-btn
                  icon="add"
                  flat
                  round
                  @click="addProductionKindToResource(resource.resource_id)"
                  :disable="!selectedKindByResource[resource.resource_id]"
                />
              </template>
            </q-select>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- Диалог создания/редактирования участка -->
    <q-dialog v-model="showCreateDialog">
      <q-card style="min-width: 400px">
        <q-card-section>
          <div class="text-h6">{{ editingResource ? 'Редактировать участок' : 'Создать участок' }}</div>
        </q-card-section>

        <q-card-section class="q-pt-none">
          <q-input
            v-model="newResource.resource_name"
            label="Название участка"
            outlined
            dense
            autofocus
            @keyup.enter="saveResource"
          />
          <q-input
            v-model.number="newResource.shift_offset"
            label="Сдвиг планирования"
            type="number"
            outlined
            dense
            class="q-mt-md"
          />
          <q-input
            v-model.number="newResource.planning_range"
            label="Диапазон планирования (дни)"
            type="number"
            outlined
            dense
            class="q-mt-md"
          />
          <q-input
            v-model.number="newResource.capacity"
            label="Мощность"
            type="number"
            outlined
            dense
            class="q-mt-md"
          />
          <q-select
            v-model="newResource.work_schedule"
            :options="workScheduleOptions"
            label="График работы"
            outlined
            dense
            class="q-mt-md"
          />
          <q-input
            v-model.number="newResource.daily_work_hours"
            label="Рабочее время в сутки (часы)"
            type="number"
            outlined
            dense
            class="q-mt-md"
          />
          <q-input
            v-model.number="newResource.buffer_days"
            label="Буфер (дней)"
            type="number"
            outlined
            dense
            class="q-mt-md"
          />
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat label="Отмена" color="primary" @click="closeDialog" />
          <q-btn
            :label="editingResource ? 'Обновить' : 'Создать'"
            color="primary"
            :loading="saving"
            :disable="!isFormValid"
            @click="saveResource"
          />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import api from '../services/api';
import { Notify } from 'quasar';

// Состояние
const resources = ref<any[]>([]);
const productionKinds = ref<any[]>([]);
const resourceProductionKinds = ref<any[]>([]);
const searchTerm = ref('');
const showCreateDialog = ref(false);
const editingResource = ref(false);
const selectedKindByResource = ref<Record<number, number | null>>({});
const saving = ref(false);

// Модель нового участка
const newResource = ref({
  resource_name: '',
  shift_offset: 0,
  planning_range: 30,
  capacity: 0,
  work_schedule: '5/2',
  daily_work_hours: 8.0,
  buffer_days: 0,
  resource_id: 0
});

// Опции графика работы
const workScheduleOptions = ref([
  '5/2', '2/2', '6/1', '7/0', 'Сменный 24/7', 'Гибкий'
]);

// Загрузка данных
onMounted(async () => {
  await loadResources();
  await loadProductionKinds();
  await loadResourceProductionKinds();
});

// Загрузка участков
const loadResources = async () => {
  try {
    const response = await api.get('/v1/resources/');
    const list = Array.isArray(response.data) ? response.data : [];
    // Нормализуем числовые поля для корректной работы v-model.number и отправки на бэкенд
    resources.value = list.map((r: any) => ({
      ...r,
      shift_offset: Number(r?.shift_offset ?? 0),
      planning_range: Number(r?.planning_range ?? 30),
      capacity: Number(r?.capacity ?? 0),
      daily_work_hours: Number(r?.daily_work_hours ?? 8.0),
      buffer_days: Number(r?.buffer_days ?? 0),
    }));
    // Синхронизируем выбранные виды на карточках (переносим прежние значения если были)
    const prev = selectedKindByResource.value || {};
    selectedKindByResource.value = Object.fromEntries(
      resources.value.map((r: any) => [r.resource_id, prev[r.resource_id] ?? null])
    );
  } catch (error: any) {
    console.error('Ошибка загрузки участков:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка загрузки участков: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Загрузка этапов
const loadProductionKinds = async () => {
  try {
    const response = await api.get('/v1/resources/production-kinds');
    const list = Array.isArray(response.data) ? response.data : [];
    productionKinds.value = list.map((k: any) => ({
      id: Number(k.id ?? k.production_kind_id ?? k.value),
      name: String(k.name ?? k.description ?? '').trim() || `ID ${k.id}`
    })).sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''));
  } catch (error: any) {
    console.error('Ошибка загрузки видов производства:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка загрузки видов производства: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Загрузка связей участок-этап
const loadResourceProductionKinds = async () => {
  // Сначала очищаем текущие связи
  resourceProductionKinds.value = [];

  const pkName = (id: number | string | null | undefined) => {
    const nid = Number(id ?? 0);
    const found = productionKinds.value.find((k: any) => Number(k.id) === nid);
    return found ? found.name : null;
  };

  // Загружаем связи для каждого участка
  for (const resource of resources.value) {
    try {
      const response = await api.get(`/v1/resources/${resource.resource_id}/production-kinds`);
      const rows = Array.isArray(response.data) ? response.data : [];
      const enriched = rows.map((r: any) => ({
        id: Number(r.id),
        resource_id: Number(r.resource_id),
        production_kind_id: Number(r.production_kind_id),
        production_kind_name: pkName(r.production_kind_id),
        created_at: r.created_at,
        updated_at: r.updated_at,
      }));
      resourceProductionKinds.value.push(...enriched);
    } catch (error: any) {
      console.error(`Ошибка загрузки видов для участка ${resource.resource_id}:`, error);
      Notify.create({
        type: 'warning',
        message: `Ошибка загрузки видов для участка ${resource.resource_id}: ${error?.response?.data?.detail || error.message || ''}`.trim()
      });
    }
  }
};

// Фильтрация участков по поисковому запросу
const filteredResources = computed(() => {
  if (!searchTerm.value) {
    return resources.value;
  }
 return resources.value.filter(resource =>
    resource.resource_name.toLowerCase().includes(searchTerm.value.toLowerCase())
  );
});

// Валидация формы
const isFormValid = computed(() => {
 return newResource.value.resource_name.trim() !== '' &&
         newResource.value.resource_name.trim().length > 0;
});

// Получение этапов для конкретного участка
const getResourceProductionKinds = (resourceId: number) => {
  return resourceProductionKinds.value.filter(r => r.resource_id === resourceId);
};

/* Получение доступных видов для конкретного участка.
   Глобальная защита от дублей: исключаем виды, которые уже назначены на ЛЮБОЙ участок. */
const availableKinds = (resourceId: number) => {
  const assignedGlobally = new Set<number>(
    (resourceProductionKinds.value || []).map((r: any) => Number(r.production_kind_id))
  );
  return (productionKinds.value || []).filter((k: any) => !assignedGlobally.has(Number(k.id)));
};

// Сброс формы
const resetForm = () => {
  newResource.value = {
    resource_name: '',
    shift_offset: 0,
    planning_range: 30,
    capacity: 0,
    work_schedule: '5/2',
    daily_work_hours: 8.0,
    buffer_days: 0,
    resource_id: 0
  };
  editingResource.value = false;
};

// Закрытие диалога
const closeDialog = () => {
  showCreateDialog.value = false;
  resetForm();
};

// Создание/редактирование участка
const saveResource = async () => {
  const name = String(newResource.value.resource_name || '').trim();
  if (!name) {
    Notify.create({ type: 'warning', message: 'Введите название участка' });
    return;
  }

  const numOr = (v: any, d: number) => (typeof v === 'number' && !isNaN(v) ? v : d);

  saving.value = true;

  try {
    if (editingResource.value) {
      const { resource_id, ...rest } = newResource.value as any;
      const payload = {
        resource_name: name,
        shift_offset: numOr(rest.shift_offset, 0),
        planning_range: numOr(rest.planning_range, 30),
        capacity: numOr(rest.capacity, 0),
        work_schedule: rest.work_schedule || '5/2',
        daily_work_hours: numOr(rest.daily_work_hours, 8.0),
        buffer_days: numOr(rest.buffer_days, 0),
      };
      await api.put(`/v1/resources/${newResource.value.resource_id}`, payload);
      Notify.create({ type: 'positive', message: 'Участок обновлен' });
    } else {
      const { resource_id, ...rest } = newResource.value as any;
      const payload = {
        resource_name: name,
        shift_offset: numOr(rest.shift_offset, 0),
        planning_range: numOr(rest.planning_range, 30),
        capacity: numOr(rest.capacity, 0),
        work_schedule: rest.work_schedule || '5/2',
        daily_work_hours: numOr(rest.daily_work_hours, 8.0),
        buffer_days: numOr(rest.buffer_days, 0),
      };
      await api.post('/v1/resources/', payload);
      Notify.create({ type: 'positive', message: 'Участок создан' });
    }

    await loadResources();
    await loadResourceProductionKinds();
    closeDialog();
  } catch (error: any) {
    console.error('Ошибка сохранения участка:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка сохранения участка: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  } finally {
    saving.value = false;
  }
};

// Редактирование участка
const editResource = (resource: any) => {
  Object.assign(newResource.value, {
    resource_name: String(resource.resource_name ?? ''),
    shift_offset: Number(resource.shift_offset ?? 0),
    planning_range: Number(resource.planning_range ?? 30),
    capacity: Number(resource.capacity ?? 0),
    work_schedule: resource.work_schedule ?? '5/2',
    daily_work_hours: Number(resource.daily_work_hours ?? 8.0),
    buffer_days: Number(resource.buffer_days ?? 0),
    resource_id: Number(resource.resource_id ?? 0)
  });
  editingResource.value = true;
  showCreateDialog.value = true;
};

// Удаление участка
const deleteResource = async (resourceId: number) => {
  if (confirm('Вы уверены, что хотите удалить этот участок?')) {
    try {
      await api.delete(`/v1/resources/${resourceId}`);
      // Очищаем выбранный вид производства для удаляемого участка
      if (selectedKindByResource.value && (resourceId in selectedKindByResource.value)) {
        delete selectedKindByResource.value[resourceId];
      }
      await loadResources();
      await loadResourceProductionKinds(); // Обновляем связи
      Notify.create({ type: 'positive', message: 'Участок удален' });
    } catch (error: any) {
      console.error('Ошибка удаления участка:', error);
      Notify.create({
        type: 'negative',
        message: `Ошибка удаления участка: ${error?.response?.data?.detail || error.message || ''}`.trim()
      });
    }
  }
};

// Обновление участка (при изменении полей)
const updateResource = async (resource: any) => {
  try {
    const numOr = (v: any, d: number) => (typeof v === 'number' && !isNaN(v) ? v : d);
    const { resource_id, ...rest } = resource;
    const name = String(rest.resource_name || '').trim();
    if (!name) {
      Notify.create({ type: 'warning', message: 'Название участка не может быть пустым' });
      return;
    }
    const payload = {
      resource_name: name,
      shift_offset: numOr(rest.shift_offset, 0),
      planning_range: numOr(rest.planning_range, 30),
      capacity: numOr(rest.capacity, 0),
      work_schedule: rest.work_schedule || '5/2',
      daily_work_hours: numOr(rest.daily_work_hours, 8.0),
      buffer_days: numOr(rest.buffer_days, 0),
    };
    await api.put(`/v1/resources/${resource.resource_id}`, payload);
    await loadResources();
    await loadResourceProductionKinds();
  } catch (error: any) {
    console.error('Ошибка обновления участка:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка обновления участка: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Добавление этапа к участку
const addProductionKindToResource = async (resourceId: number) => {
  const selected = selectedKindByResource.value[resourceId];
  if (!selected) return;

  try {
    await api.post(`/v1/resources/${resourceId}/production-kinds`, {
      resource_id: resourceId,
      production_kind_id: selected
    });
    await loadResourceProductionKinds(); // Обновляем связи
    selectedKindByResource.value[resourceId] = null;
    Notify.create({ type: 'positive', message: 'Вид производства добавлен к участку' });
  } catch (error: any) {
    console.error('Ошибка добавления вида производства к участку:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка добавления вида производства к участку: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Удаление этапа из участка
const removeProductionKindFromResource = async (resourceId: number, productionKindId: number) => {
  try {
    await api.delete(`/v1/resources/${resourceId}/production-kinds/${productionKindId}`);
    await loadResourceProductionKinds(); // Обновляем связи
    Notify.create({ type: 'positive', message: 'Вид производства удален из участка' });
  } catch (error: any) {
    console.error('Ошибка удаления вида производства из участка:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка удаления вида производства из участка: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};
// Обработка изменения выбора этапа
/* зарезервировано под возможные обработчики выбора вида производства */
</script>

<style scoped>
.resource-card {
  min-height: 400px;
 display: flex;
  flex-direction: column;
}
</style>