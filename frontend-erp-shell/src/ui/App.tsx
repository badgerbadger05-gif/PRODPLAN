import { NavLink, Route, Routes } from 'react-router-dom'
import { HomePage } from './pages/HomePage'
import { MrpResultPage } from './pages/MrpResultPage'
import { MrpRunsPage } from './pages/MrpRunsPage'
import { PeriodPlanPage } from './pages/PeriodPlanPage'
import { ProductionControlPage } from './pages/ProductionControlPage'
import { ProductionReportWeekPage } from './pages/ProductionReportWeekPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { SpecificationPage } from './pages/SpecificationPage'
import { StageDistributionPage } from './pages/StageDistributionPage'
import { SyncPage } from './pages/SyncPage'

const navItems = [
  { to: '/', title: 'Главная', end: true },
  { to: '/period-plan', title: 'Планирование выпуска' },
  { to: '/production-control', title: 'Журнал заказов' },
  { to: '/production-report-week', title: 'Выпуск недельный' },
  { to: '/mrp-runs', title: 'MRP прогоны' },
  { to: '/resources', title: 'Ресурсы' },
  { to: '/stage-distribution', title: 'Распределение этапов' },
  { to: '/specification', title: 'Спецификации' },
  { to: '/sync', title: 'Синхронизация' },
]

export function App() {
  return (
    <div className="app">
      <aside className="nav">
        <div className="brand">
          <div className="brandMark">P</div>
          <div>
            <strong>PRODPLAN</strong>
            <span>ERP shell</span>
          </div>
        </div>
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `navItem${isActive ? ' active' : ''}`}
          >
            {item.title}
          </NavLink>
        ))}
      </aside>

      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/period-plan" element={<PeriodPlanPage />} />
        <Route path="/period-plan/:planId" element={<PeriodPlanPage />} />
        <Route path="/production-control" element={<ProductionControlPage />} />
        <Route path="/production-report-week" element={<ProductionReportWeekPage />} />
        <Route path="/mrp-runs" element={<MrpRunsPage />} />
        <Route path="/mrp-runs/:runId" element={<MrpResultPage />} />
        <Route path="/resources" element={<ResourcesPage />} />
        <Route path="/stage-distribution" element={<StageDistributionPage />} />
        <Route path="/specification" element={<SpecificationPage />} />
        <Route path="/sync" element={<SyncPage />} />
      </Routes>
    </div>
  )
}
