import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'
import MRPResultPage from '../pages/MRPResultPage.vue'

// Динамический импорт компонентов
const Index = () => import('../pages/Index.vue')
  const PlanQuarterlyPage = () => import('../pages/PlanQuarterlyPage.vue')
  const ProductionReportWeekPage = () => import('../pages/ProductionReportWeekPage.vue')
  const SyncPage = () => import('../pages/SyncPage.vue')
  const StagesPage = () => import('../pages/StagesPage.vue')
  const SpecificationPage = () => import('../pages/SpecificationPage.vue')
  const ResourcesPage = () => import('../pages/ResourcesPage.vue')
  const ProductionControlPage = () => import('../pages/ProductionControlPage.vue')

const MRPRunsPage = () => import('../pages/MRPRunsPage.vue')

const routes = [
  {
    path: '/',
    component: MainLayout,
    children: [
      { path: '', name: 'home', component: Index },
      { path: 'plan/quarterly', name: 'plan-quarterly', component: PlanQuarterlyPage },
      { path: 'plan/production-report/week', name: 'production-report-week', component: ProductionReportWeekPage },
      { path: 'sync', name: 'sync', component: SyncPage },
      { path: 'stages', name: 'stages', component: StagesPage },
      { path: 'specification', name: 'specification', component: SpecificationPage },
      { path: 'resources', name: 'resources', component: ResourcesPage },
      { path: 'production-control', name: 'production-control', component: ProductionControlPage },
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

// DIAGNOSTICS: логируем переходы роутера (временно)
try {
  router.beforeEach((to, from, next) => {
    console.log('[router.beforeEach]', { to: to.fullPath, name: to.name, params: to.params })
    next()
  })
  router.afterEach((to) => {
    console.log('[router.afterEach]', { to: to.fullPath, name: to.name })
  })
} catch (e) {
  // no-op
}

export default router
