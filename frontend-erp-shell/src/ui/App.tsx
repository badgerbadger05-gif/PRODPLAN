import { lazy, Suspense, useMemo } from 'react'
import { Link, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { ErrorBoundary } from './ErrorBoundary'
import { KeyboardShortcutShell, type KeyboardShortcut } from './platform'
import { frontendResources } from './resourceRegistry'

const DbrDrumBoardPage = lazy(() => import('./pages/DbrDrumBoardPage').then((module) => ({ default: module.DbrDrumBoardPage })))
const DbrFeederPage = lazy(() => import('./pages/DbrFeederPage').then((module) => ({ default: module.DbrFeederPage })))
const DbrProgramsPage = lazy(() => import('./pages/DbrProgramsPage').then((module) => ({ default: module.DbrProgramsPage })))
const DbrPurchasePage = lazy(() => import('./pages/DbrPurchasePage').then((module) => ({ default: module.DbrPurchasePage })))
const DbrSettingsPage = lazy(() => import('./pages/DbrSettingsPage').then((module) => ({ default: module.DbrSettingsPage })))
const HomePage = lazy(() => import('./pages/HomePage').then((module) => ({ default: module.HomePage })))
const MrpResultPage = lazy(() => import('./pages/MrpResultPage').then((module) => ({ default: module.MrpResultPage })))
const MrpRunsPage = lazy(() => import('./pages/MrpRunsPage').then((module) => ({ default: module.MrpRunsPage })))
const PeriodPlanPage = lazy(() => import('./pages/PeriodPlanPage').then((module) => ({ default: module.PeriodPlanPage })))
const ProductionControlPage = lazy(() => import('./pages/ProductionControlPage').then((module) => ({ default: module.ProductionControlPage })))
const ProductionReportWeekPage = lazy(() => import('./pages/ProductionReportWeekPage').then((module) => ({ default: module.ProductionReportWeekPage })))
const PurchaseControlPage = lazy(() => import('./pages/PurchaseControlPage').then((module) => ({ default: module.PurchaseControlPage })))
const ResourcesPage = lazy(() => import('./pages/ResourcesPage').then((module) => ({ default: module.ResourcesPage })))
const SpecificationPage = lazy(() => import('./pages/SpecificationPage').then((module) => ({ default: module.SpecificationPage })))
const StageDistributionPage = lazy(() => import('./pages/StageDistributionPage').then((module) => ({ default: module.StageDistributionPage })))
const SyncPage = lazy(() => import('./pages/SyncPage').then((module) => ({ default: module.SyncPage })))
const TransferRequestsPage = lazy(() => import('./pages/TransferRequestsPage').then((module) => ({ default: module.TransferRequestsPage })))
const WorkshopBindingReviewPage = lazy(() => import('./pages/WorkshopBindingReviewPage').then((module) => ({ default: module.WorkshopBindingReviewPage })))

function RouteLoading() {
  return <main className="workArea"><div className="hintLine">Загрузка раздела...</div></main>
}

export function App() {
  const navigate = useNavigate()
  const navigationShortcuts = useMemo<KeyboardShortcut[]>(
    () => frontendResources
      .filter((resource) => resource.shortcut)
      .map((resource) => ({
        id: `navigate-${resource.name}`,
        keys: resource.shortcut!,
        run: () => navigate(resource.to),
      })),
    [navigate],
  )

  return (
    <div className="app">
      <KeyboardShortcutShell shortcuts={navigationShortcuts} />
      <aside className="nav">
        <div className="brand">
          <div className="brandMark">P</div>
          <div>
            <strong>PRODPLAN</strong>
            <span>ERP shell</span>
          </div>
        </div>
        {frontendResources.map((resource) => (
          <NavLink
            key={resource.name}
            to={resource.to}
            end={resource.end}
            className={({ isActive }) => `navItem${isActive ? ' active' : ''}`}
          >
            {resource.title}
            {resource.shortcut && <span className="navShortcut">{resource.shortcut}</span>}
          </NavLink>
        ))}
        <div className="navLogoSlot" aria-label="Логотип компании ЗСМ">
          <img src="/zsm-logo-sidebar.png" alt="ЗСМ" />
        </div>
      </aside>

      <ErrorBoundary>
        <Suspense fallback={<RouteLoading />}>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/period-plan" element={<PeriodPlanPage />} />
            <Route path="/period-plan/:planId" element={<PeriodPlanPage />} />
            <Route path="/dbr" element={<DbrDrumBoardPage />} />
            <Route path="/dbr/programs" element={<DbrProgramsPage />} />
            <Route path="/dbr/feeder" element={<DbrFeederPage />} />
            <Route path="/dbr/purchase" element={<DbrPurchasePage />} />
            <Route path="/dbr/settings" element={<DbrSettingsPage />} />
            <Route path="/production-control" element={<ProductionControlPage />} />
            <Route path="/purchase-control" element={<PurchaseControlPage />} />
            <Route path="/transfer-requests" element={<TransferRequestsPage />} />
            <Route path="/production-report-week" element={<ProductionReportWeekPage />} />
            <Route path="/mrp-runs" element={<MrpRunsPage />} />
            <Route path="/mrp-runs/:runId" element={<MrpResultPage />} />
            <Route path="/resources" element={<ResourcesPage />} />
            <Route path="/workshop-binding-review" element={<WorkshopBindingReviewPage />} />
            <Route path="/stage-distribution" element={<StageDistributionPage />} />
            <Route path="/specification" element={<SpecificationPage />} />
            <Route path="/sync" element={<SyncPage />} />
            <Route
              path="*"
              element={(
                <main className="workArea">
                  <div className="errorLine">Страница не найдена</div>
                  <Link to="/" className="navItem">На главную</Link>
                </main>
              )}
            />
          </Routes>
        </Suspense>
      </ErrorBoundary>
    </div>
  )
}
