<template>
  <q-page class="q-pa-lg">
    <!-- Приветственный блок -->
    <div style="text-align: center; margin-bottom: 40px;">
      <h1 style="color: #1976d2; margin-bottom: 20px;">PRODPLAN</h1>
      <h2 style="color: #666; margin-bottom: 30px;">Система планирования производства</h2>
      <p style="font-size: 16px; margin-bottom: 40px;">
        Комплексное решение для управления производственными процессами,
        планирования заказов и синхронизации данных с 1С
      </p>
    </div>

    <!-- Основные функции -->
    <div class="row q-gutter-lg q-mb-xl justify-center">
      <div class="col-12 col-md-5 col-lg-3">
        <q-card class="feature-card">
          <q-card-section class="text-center">
            <q-icon name="assessment" size="3rem" color="primary" class="q-mb-md" />
            <div class="text-h6 q-mb-sm">Планирование производства</div>
            <div class="text-body2 text-grey-6">
              Создавайте и управляйте планами производства на 30 дней вперед
            </div>
          </q-card-section>
        </q-card>
      </div>

      <div class="col-12 col-md-5 col-lg-3">
        <q-card class="feature-card">
          <q-card-section class="text-center">
            <q-icon name="sync" size="3rem" color="secondary" class="q-mb-md" />
            <div class="text-h6 q-mb-sm">Синхронизация с 1С</div>
            <div class="text-body2 text-grey-6">
              Автоматическая синхронизация остатков и данных через OData API
            </div>
          </q-card-section>
        </q-card>
      </div>

      <div class="col-12 col-md-5 col-lg-3">
        <q-card class="feature-card">
          <q-card-section class="text-center">
            <q-icon name="calculate" size="3rem" color="positive" class="q-mb-md" />
            <div class="text-h6 q-mb-sm">Расчет заказов</div>
            <div class="text-body2 text-grey-6">
              Автоматический расчет потребностей в материалах и комплектующих
            </div>
          </q-card-section>
        </q-card>
      </div>

      <div class="col-12 col-md-5 col-lg-3">
        <q-card class="feature-card">
          <q-card-section class="text-center">
            <q-icon name="history" size="3rem" color="info" class="q-mb-md" />
            <div class="text-h6 q-mb-sm">История и аналитика</div>
            <div class="text-body2 text-grey-6">
              Отслеживание истории остатков и анализ производственных трендов
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- Статус системы -->
    <div class="row justify-center q-mb-xl">
      <div class="col-12 col-md-8">
        <q-card>
          <q-card-section>
            <div class="text-h6 q-mb-md">Статус системы</div>
            <div class="row q-gutter-lg">
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <q-icon name="check_circle" size="2rem" color="positive" class="q-mb-sm" />
                <div class="text-body2 text-grey-6">Frontend</div>
                <div class="text-caption text-positive">Работает</div>
              </div>
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <q-icon name="check_circle" size="2rem" color="positive" class="q-mb-sm" />
                <div class="text-body2 text-grey-6">Backend API</div>
                <div class="text-caption text-positive">Готов</div>
              </div>
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <q-icon name="check_circle" size="2rem" color="positive" class="q-mb-sm" />
                <div class="text-body2 text-grey-6">База данных</div>
                <div class="text-caption text-positive">Подключена</div>
              </div>
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <q-icon name="info" size="2rem" color="grey-5" class="q-mb-sm" />
                <div class="text-body2 text-grey-6">1С интеграция</div>
                <div class="text-caption text-grey-5">Настройка</div>
              </div>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- Статистика -->
    <div class="row justify-center q-mb-xl">
      <div class="col-12 col-md-8">
        <q-card>
          <q-card-section>
            <div class="text-h6 q-mb-md">Система</div>
            <div class="row q-gutter-lg">
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <div class="text-h4 text-primary">{{ stats.itemsCount || 0 }}</div>
                <div class="text-body2 text-grey-6">Изделий</div>
              </div>
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <div class="text-h4 text-secondary">{{ stats.stagesCount || 0 }}</div>
                <div class="text-body2 text-grey-6">Этапов</div>
              </div>
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <div class="text-h4 text-positive">{{ stats.planEntriesCount || 0 }}</div>
                <div class="text-body2 text-grey-6">Записей плана</div>
              </div>
              <div class="col-12 col-sm-6 col-md-3 text-center">
                <div class="text-h4 text-info">{{ stats.lastSync ? formatDate(stats.lastSync) : 'Никогда' }}</div>
                <div class="text-body2 text-grey-6">Последняя синхронизация</div>
              </div>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- Быстрые действия -->
    <div class="row justify-center q-mb-xl">
      <div class="col-12 col-md-8">
        <q-card>
          <q-card-section>
            <div class="text-h6 q-mb-md">Быстрое начало</div>
            <div class="row q-gutter-md">
              <q-btn
                color="primary"
                icon="assessment"
                label="План производства"
                push
                @click="$router.push('/plan')"
                class="col-12 col-sm-auto"
              />
              <q-btn
                color="secondary"
                icon="sync"
                label="Синхронизация"
                push
                @click="$router.push('/sync')"
                class="col-12 col-sm-auto"
              />
              <q-btn
                color="positive"
                icon="calculate"
                label="Расчет заказов"
                push
                @click="$router.push('/orders')"
                class="col-12 col-sm-auto"
              />
              <q-btn
                color="info"
                icon="settings"
                label="Настройки"
                push
                @click="$router.push('/settings')"
                class="col-12 col-sm-auto"
              />
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- Основные действия -->
    <div class="row justify-center q-mb-xl">
      <div class="col-12 col-md-8">
        <q-card>
          <q-card-section>
            <div class="text-h6 q-mb-md">Основные действия</div>
            <div class="row q-gutter-md">
              <q-btn
                color="primary"
                icon="assessment"
                label="План производства"
                push
                size="lg"
                @click="$router.push('/plan')"
                class="col-12 col-sm-5"
              />
              <q-btn
                color="secondary"
                icon="sync"
                label="Синхронизация"
                push
                size="lg"
                @click="$router.push('/sync')"
                class="col-12 col-sm-5"
              />
            </div>
            <q-separator class="q-my-md" />
            <div class="row q-gutter-md">
              <q-btn
                color="positive"
                icon="calculate"
                label="Расчет заказов"
                push
                @click="$router.push('/orders')"
                class="col-12 col-sm-5"
              />
              <q-btn
                color="info"
                icon="settings"
                label="Настройки"
                push
                @click="$router.push('/settings')"
                class="col-12 col-sm-5"
              />
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>

    <!-- Информация о системе -->
    <div class="row justify-center">
      <div class="col-12 col-md-8">
        <q-card>
          <q-card-section>
            <div class="text-h6 q-mb-md">О системе</div>
            <div class="row q-gutter-lg">
              <div class="col-12 col-md-6">
                <div class="text-body2">
                  <strong>Версия:</strong> 1.6<br>
                  <strong>Frontend:</strong> Quasar Vue.js 3 + TypeScript<br>
                  <strong>Backend:</strong> FastAPI + PostgreSQL<br>
                  <strong>Интеграция:</strong> 1С OData API
                </div>
              </div>
              <div class="col-12 col-md-6">
                <div class="text-body2">
                  <strong>Основные возможности:</strong><br>
                  • Управление спецификациями (BOM)<br>
                  • Планирование производства<br>
                  • Расчет потребностей в материалах<br>
                  • Синхронизация с 1С<br>
                  • Генерация Excel отчетов
                </div>
              </div>
            </div>
          </q-card-section>
        </q-card>
      </div>
    </div>
</q-page>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'

// Роутер для навигации
const $router = useRouter()

// Статистика системы
const stats = ref({
  itemsCount: 0,
  stagesCount: 0,
  planEntriesCount: 0,
  lastSync: null as string | null
})

// Форматирование даты
const formatDate = (dateStr: string) => {
  return new Date(dateStr).toLocaleDateString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  })
}

// Загрузка статистики (заглушка для демонстрации)
onMounted(() => {
  // Здесь будет API вызов для получения реальной статистики
  // Пока используем демо-данные
  stats.value = {
    itemsCount: 1247,
    stagesCount: 8,
    planEntriesCount: 3421,
    lastSync: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString() // 2 часа назад
  }
})
  
</script>

<style scoped>
.feature-card {
  transition: transform 0.2s;
  cursor: pointer;
}

.feature-card:hover {
  transform: translateY(-4px);
}

.plan-table {
  min-height: 400px;
}
</style>