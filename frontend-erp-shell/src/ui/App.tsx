import { lazy, Suspense, useMemo, type ReactNode } from 'react'
import { Link, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { ErrorBoundary } from './ErrorBoundary'
import { KeyboardShortcutShell, type KeyboardShortcut } from './platform'
import { frontendResources } from './resourceRegistry'
import { canAccessResource } from './resourceRegistry'
import { LoginPage, useSession } from './session'
import { DeploymentContourBanner } from './DeploymentContourBanner'

const DbrDrumBoardPage = lazy(() => import('./pages/DbrDrumBoardPage').then((module) => ({ default: module.DbrDrumBoardPage })))
const DbrFeederPage = lazy(() => import('./pages/DbrFeederPage').then((module) => ({ default: module.DbrFeederPage })))
const DbrProgramsPage = lazy(() => import('./pages/DbrProgramsPage').then((module) => ({ default: module.DbrProgramsPage })))
const DbrPurchasePage = lazy(() => import('./pages/DbrPurchasePage').then((module) => ({ default: module.DbrPurchasePage })))
const DbrSettingsPage = lazy(() => import('./pages/DbrSettingsPage').then((module) => ({ default: module.DbrSettingsPage })))
const HomePage = lazy(() => import('./pages/HomePage').then((module) => ({ default: module.HomePage })))
const LedgerWorkspaceRoute = lazy(() => import('./pages/LedgerWorkspacePage').then((module) => ({ default: module.LedgerWorkspaceRoute })))
const MrpResultPage = lazy(() => import('./pages/MrpResultPage').then((module) => ({ default: module.MrpResultPage })))
const MrpRunsPage = lazy(() => import('./pages/MrpRunsPage').then((module) => ({ default: module.MrpRunsPage })))
const PeriodPlanPage = lazy(() => import('./pages/PeriodPlanPage').then((module) => ({ default: module.PeriodPlanPage })))
const PlanningComparisonPage = lazy(() => import('./pages/PlanningComparisonPage').then((module) => ({ default: module.PlanningComparisonPage })))
const ProductionControlPage = lazy(() => import('./pages/ProductionControlPage').then((module) => ({ default: module.ProductionControlPage })))
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
  const session = useSession()
  const navigate = useNavigate()
  const accessibleResources = useMemo(
    () => frontendResources.filter((resource) => session.user && canAccessResource(resource, session.user.roles)),
    [session.user],
  )
  const navigationShortcuts = useMemo<KeyboardShortcut[]>(
    () => accessibleResources
      .filter((resource) => resource.shortcut)
      .map((resource) => ({
        id: `navigate-${resource.name}`,
        keys: resource.shortcut!,
        run: () => navigate(resource.to),
      })),
    [accessibleResources, navigate],
  )

  if (session.loading) return <main className="workArea"><div className="hintLine">Загрузка сессии...</div></main>
  if (!session.user) return <LoginPage />

  const guard = (name: string, content: ReactNode) => accessibleResources.some((resource) => resource.name === name)
    ? content
    : <main className="workArea"><div className="errorLine" role="alert">Нет доступа к разделу</div></main>

  return (
    <div className="app">
      <DeploymentContourBanner />
      <KeyboardShortcutShell shortcuts={navigationShortcuts} />
      <aside className="nav">
        <div className="brand">
          <div className="brandMark">P</div>
          <div>
            <strong>PRODPLAN</strong>
            <span>ERP shell</span>
          </div>
        </div>
        {accessibleResources.map((resource) => (
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

      <div className="sessionBadge sessionBadgeTop">
        <strong>{session.user.name}</strong>
        <button onClick={() => void session.logout()}>Выйти</button>
      </div>

      <ErrorBoundary>
        <Suspense fallback={<RouteLoading />}>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/period-plan" element={guard('period_plan', <PeriodPlanPage />)} />
            <Route path="/period-plan/:planId" element={guard('period_plan', <PeriodPlanPage />)} />
            <Route path="/planning-comparison" element={guard('planning_comparison', <PlanningComparisonPage />)} />
            <Route path="/dbr" element={guard('dbr', <DbrDrumBoardPage />)} />
            <Route path="/dbr/programs" element={guard('dbr', <DbrProgramsPage />)} />
            <Route path="/dbr/feeder" element={guard('dbr', <DbrFeederPage />)} />
            <Route path="/dbr/purchase" element={guard('dbr', <DbrPurchasePage />)} />
            <Route path="/dbr/settings" element={guard('dbr', <DbrSettingsPage />)} />
            <Route path="/production-control" element={guard('production_order', <ProductionControlPage />)} />
            <Route path="/purchase-control" element={guard('purchase_order', <PurchaseControlPage />)} />
            <Route path="/transfer-requests" element={guard('material_transfer', <TransferRequestsPage />)} />
            <Route path="/mrp-runs" element={guard('plan_run', <MrpRunsPage />)} />
            <Route path="/mrp-runs/:runId" element={guard('plan_run', <MrpResultPage />)} />
            <Route path="/ledger" element={guard('ledger', <LedgerWorkspaceRoute />)} />
            <Route path="/ledger/items/:itemId" element={guard('ledger', <LedgerWorkspaceRoute />)} />
            <Route path="/resources" element={guard('resources', <ResourcesPage />)} />
            <Route path="/workshop-binding-review" element={guard('workshop_binding', <WorkshopBindingReviewPage />)} />
            <Route path="/stage-distribution" element={guard('stage_distribution', <StageDistributionPage />)} />
            <Route path="/specification" element={guard('specification', <SpecificationPage />)} />
            <Route path="/sync" element={guard('sync', <SyncPage />)} />
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
