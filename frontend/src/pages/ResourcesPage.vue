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
            </div>
          </q-card-section>

          <q-card-section>
            <div class="text-subtitle2 q-mb-sm">Этапы производства:</div>
            <q-chip
              v-for="stage in getResourceStages(resource.resource_id)"
              :key="stage.id"
              removable
              @remove="removeStageFromResource(resource.resource_id, stage.stage_id)"
            >
              {{ stage.stage_name }}
            </q-chip>
            
            <q-select
              v-model="selectedStageByResource[resource.resource_id]"
              :options="availableStages(resource.resource_id)"
              option-label="stage_name"
              option-value="stage_id"
              emit-value
              map-options
              label="Добавить этап"
              outlined
              dense
              class="q-mt-md"
              @update:model-value="stageSelectionChanged(resource.resource_id)"
            >
              <template v-slot:after>
                <q-btn
                  icon="add"
                  flat
                  round
                  @click="addStageToResource(resource.resource_id)"
                  :disable="!selectedStageByResource[resource.resource_id]"
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
const stages = ref<any[]>([]);
const resourceStages = ref<any[]>([]);
const searchTerm = ref('');
const showCreateDialog = ref(false);
const editingResource = ref(false);
const selectedStageByResource = ref<Record<number, number | null>>({});
const saving = ref(false);

// Модель нового участка
const newResource = ref({
  resource_name: '',
  shift_offset: 0,
  planning_range: 30,
  capacity: 0,
  work_schedule: '5/2',
  daily_work_hours: 8.0,
  resource_id: 0
});

// Опции графика работы
const workScheduleOptions = ref([
  '5/2', '2/2', '6/1', '7/0', 'Сменный 24/7', 'Гибкий'
]);

// Загрузка данных
onMounted(async () => {
 await loadResources();
  await loadStages();
  await loadResourceStages();
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
    }));
    // Синхронизируем выбранные этапы на карточках (переносим прежние значения если были)
    const prev = selectedStageByResource.value || {};
    selectedStageByResource.value = Object.fromEntries(
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
const loadStages = async () => {
  try {
    const response = await api.get('/v1/plan/stages');
    const raw = Array.isArray(response.data) ? response.data : (response.data?.stages || []);
    stages.value = raw.map((s: any) => ({
      stage_id: s.stage_id ?? s.id ?? s.value,
      stage_name: s.stage_name ?? s.name ?? s.label ?? String(s.stage_id ?? s.id ?? s.value),
    }));
  } catch (error: any) {
    console.error('Ошибка загрузки этапов:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка загрузки этапов: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Загрузка связей участок-этап
const loadResourceStages = async () => {
  // Сначала очищаем текущие связи
  resourceStages.value = [];
  
  // Загружаем связи для каждого участка
  for (const resource of resources.value) {
    try {
      const response = await api.get(`/v1/resources/${resource.resource_id}/stages`);
      const stagesWithInfo = response.data.map((rs: any) => {
        const stageInfo = stages.value.find((s: any) => s.stage_id === rs.stage_id);
        return {
          ...rs,
          stage_name: stageInfo ? stageInfo.stage_name : 'Неизвестный этап'
        };
      });
      resourceStages.value.push(...stagesWithInfo);
    } catch (error: any) {
      console.error(`Ошибка загрузки этапов для участка ${resource.resource_id}:`, error);
      Notify.create({
        type: 'warning',
        message: `Ошибка загрузки этапов для участка ${resource.resource_id}: ${error?.response?.data?.detail || error.message || ''}`.trim()
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
const getResourceStages = (resourceId: number) => {
  return resourceStages.value.filter(rs => rs.resource_id === resourceId);
};

/* Получение доступных этапов для участка (глобально не привязанных ни к одному участку)
   Защита от дурака: один этап может быть назначен только одному участку.
   Если этап уже присвоен к какому-то участку, он пропадает из списка на добавление у всех. */
const availableStages = (resourceId: number) => {
  // Собираем множество всех уже назначенных этапов по всем участкам
  const globallyAssigned = new Set<number>(
    (resourceStages.value || []).map((rs: any) => Number(rs.stage_id))
  );

  // В список добавления попадают только те этапы, которые ещё нигде не назначены
  return (stages.value || []).filter((stage: any) => !globallyAssigned.has(Number(stage.stage_id)));
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
      };
      await api.post('/v1/resources/', payload);
      Notify.create({ type: 'positive', message: 'Участок создан' });
    }

    await loadResources();
    await loadResourceStages();
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
      // Очищаем выбранный этап для удаляемого участка
      if (selectedStageByResource.value && resourceId in selectedStageByResource.value) {
        delete selectedStageByResource.value[resourceId];
      }
      await loadResources();
      await loadResourceStages(); // Обновляем связи
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
    };
    await api.put(`/v1/resources/${resource.resource_id}`, payload);
    await loadResources();
    await loadResourceStages();
  } catch (error: any) {
    console.error('Ошибка обновления участка:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка обновления участка: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Добавление этапа к участку
const addStageToResource = async (resourceId: number) => {
  const selected = selectedStageByResource.value[resourceId];
  if (!selected) return;
  
  try {
    await api.post(`/v1/resources/${resourceId}/stages`, {
      resource_id: resourceId,
      stage_id: selected
    });
    
    await loadResourceStages(); // Обновляем связи
    selectedStageByResource.value[resourceId] = null;
    Notify.create({ type: 'positive', message: 'Этап добавлен к участку' });
  } catch (error: any) {
    console.error('Ошибка добавления этапа к участку:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка добавления этапа к участку: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};

// Удаление этапа из участка
const removeStageFromResource = async (resourceId: number, stageId: number) => {
  try {
    await api.delete(`/v1/resources/${resourceId}/stages/${stageId}`);
    await loadResourceStages(); // Обновляем связи
    Notify.create({ type: 'positive', message: 'Этап удален из участка' });
  } catch (error: any) {
    console.error('Ошибка удаления этапа из участка:', error);
    Notify.create({
      type: 'negative',
      message: `Ошибка удаления этапа из участка: ${error?.response?.data?.detail || error.message || ''}`.trim()
    });
  }
};
// Обработка изменения выбора этапа
const stageSelectionChanged = (resourceId: number) => {
  // При изменении выбора этапа, ничего дополнительно делать не нужно,
  // так как значение уже сохраняется в selectedStage
};
</script>

<style scoped>
.resource-card {
  min-height: 400px;
 display: flex;
  flex-direction: column;
}
</style>