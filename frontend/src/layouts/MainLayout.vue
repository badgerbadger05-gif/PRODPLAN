<template>
  <q-layout view="HHh LpR FFF" class="main-layout">
    <q-header elevated height-hint="64">
      <q-toolbar>
        <q-btn
          flat
          dense
          round
          icon="menu"
          aria-label="Menu"
          @click="toggleLeftDrawer"
        />

        <q-toolbar-title>
          PRODPLAN
        </q-toolbar-title>

        <q-space />

        <q-btn flat round icon="notifications" class="q-mr-sm">
          <q-badge color="red" floating>3</q-badge>
        </q-btn>
      </q-toolbar>
    </q-header>

    <q-drawer
      v-model="leftDrawerOpen"
      show-if-above
      bordered
      :width="250"
      :breakpoint="500"
    >
      <q-scroll-area class="fit">
        <!-- Заголовок боковой панели -->
        <div class="q-pa-md text-center drawer-header">
          <div class="row items-center justify-between">
            <div>
              <div class="text-h6 text-weight-bold">PRODPLAN</div>
              <div class="text-caption text-grey">Система планирования производства</div>
            </div>
            <q-btn
              flat
              dense
              round
              :icon="leftDrawerOpen ? 'chevron_left' : 'chevron_right'"
              @click="toggleLeftDrawer"
              aria-label="Свернуть меню"
            />
          </div>
        </div>

        <q-separator />

        <q-list>
          <q-item clickable v-ripple to="/" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="home" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Главная</q-item-label>
              <q-item-label caption>Обзор системы</q-item-label>
            </q-item-section>
          </q-item>
 
          <q-item clickable v-ripple to="/plan" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="assessment" />
            </q-item-section>
            <q-item-section>
              <q-item-label>План выпуска техники</q-item-label>
              <q-item-label caption>Управление планами</q-item-label>
            </q-item-section>
          </q-item>
 
          <!-- Этапы производства -->
          <q-item clickable v-ripple to="/stages" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="precision_manufacturing" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Этапы производства</q-item-label>
              <q-item-label caption>Развертка спецификаций по этапам</q-item-label>
            </q-item-section>
          </q-item>
 
          <q-item clickable v-ripple to="/sync" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="sync" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Синхронизация</q-item-label>
              <q-item-label caption>Данные из 1С</q-item-label>
            </q-item-section>
          </q-item>

          <q-item clickable v-ripple to="/orders" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="calculate" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Расчет заказов</q-item-label>
              <q-item-label caption>Потребности в материалах</q-item-label>
            </q-item-section>
          </q-item>
 
          <q-separator class="q-mt-md q-mb-xs" />
 
          <div class="text-caption text-grey q-px-md q-pt-sm">Настройки</div>
 
          <q-item clickable v-ripple to="/settings" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="settings" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Настройки</q-item-label>
              <q-item-label caption>Конфигурация системы</q-item-label>
            </q-item-section>
          </q-item>

          <q-item clickable v-ripple to="/reports" exact class="menu-item">
            <q-item-section avatar>
              <q-icon name="assessment" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Отчеты</q-item-label>
              <q-item-label caption>Аналитика и статистика</q-item-label>
            </q-item-section>
          </q-item>
        </q-list>
      </q-scroll-area>
    </q-drawer>

    <q-page-container class="page-container">
      <router-view />
    </q-page-container>
    
    <q-footer elevated class="bg-primary text-white" height-hint="48">
      <q-toolbar>
        <q-toolbar-title class="text-center">
          <div class="text-caption">© 2025 PRODPLAN - Система планирования производства</div>
        </q-toolbar-title>
      </q-toolbar>
    </q-footer>
  </q-layout>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'

// Состояние бокового меню
const leftDrawerOpen = ref(false)

// Загрузка состояния меню из localStorage при инициализации
onMounted(() => {
  const savedState = localStorage.getItem('leftDrawerOpen')
  if (savedState !== null) {
    leftDrawerOpen.value = JSON.parse(savedState)
  }
})

// Сохранение состояния меню в localStorage при изменении
watch(leftDrawerOpen, (newValue) => {
  localStorage.setItem('leftDrawerOpen', JSON.stringify(newValue))
})

const toggleLeftDrawer = () => {
  leftDrawerOpen.value = !leftDrawerOpen.value
}

function getPageStyle(offset: number, height: number) {
  return {
    height: `${height - offset}px`,
    maxHeight: `${height - offset}px`
  }
}
</script>

<style scoped>
.menu-item {
  border-left: 4px solid transparent;
  transition: all 0.3s ease;
}

.menu-item:hover {
  background-color: rgba(0, 0, 0, 0.05);
  border-left: 4px solid #1976d2;
}

.menu-item--active {
  background-color: rgba(25, 118, 210, 0.1);
  border-left: 4px solid #1976d2;
}

.drawer-header {
  background-color: #f5f5f5;
  border-bottom: 1px solid #e0e0e0;
}
</style>
