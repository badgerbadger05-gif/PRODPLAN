import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'

// Динамический импорт компонентов
const Index = () => import('../pages/Index.vue')
const PlanPage = () => import('../pages/PlanPage.vue')
const PlanQuarterlyPage = () => import('../pages/PlanQuarterlyPage.vue')
const SyncPage = () => import('../pages/SyncPage.vue')
const StagesPage = () => import('../pages/StagesPage.vue')
const SpecificationPage = () => import('../pages/SpecificationPage.vue')
const ResourcesPage = () => import('../pages/ResourcesPage.vue')

const MRPRunsPage = () => import('../pages/MRPRunsPage.vue')
const MRPResultPage = () => import('../pages/MRPResultPage.vue')
// Заглушки для будущих страниц
const OrdersPage = { template: '<div class="q-pa-lg"><h4>Расчет заказов</h4><p>Страница находится в разработке</p></div>' }
const SettingsPage = { template: '<div class="q-pa-lg"><h4>Настройки</h4><p>Страница находится в разработке</p></div>' }
const ReportsPage = { template: '<div class="q-pa-lg"><h4>Отчеты</h4><p>Страница находится в разработке</p></div>' }

const routes = [
  {
    path: '/',
    component: MainLayout,
    children: [
      { path: '', name: 'home', component: Index },
      { path: 'plan', name: 'plan', component: PlanPage },
      { path: 'plan/quarterly', name: 'plan-quarterly', component: PlanQuarterlyPage },
      { path: 'sync', name: 'sync', component: SyncPage },
      { path: 'stages', name: 'stages', component: StagesPage },
      { path: 'orders', name: 'orders', component: OrdersPage },
      { path: 'settings', name: 'settings', component: SettingsPage },
      { path: 'reports', name: 'reports', component: ReportsPage },
      { path: 'specification', name: 'specification', component: SpecificationPage },
      { path: 'resources', name: 'resources', component: ResourcesPage },
      { path: 'mrp', name: 'mrp-runs', component: MRPRunsPage },
      { path: 'mrp/:runId', name: 'mrp-result', component: MRPResultPage, props: true }
    ]
 }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior(to, from, savedPosition) {
    // Всегда прокручивать к верху при переходе на новую страницу
    if (savedPosition) {
      return savedPosition
    } else {
      return { top: 0 }
    }
  }
})

export default router