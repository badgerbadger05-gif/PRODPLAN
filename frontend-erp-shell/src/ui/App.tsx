import { useState } from 'react'
import type { PlanningRunRow } from '../domain/planning'
import { MrpResultPage } from './pages/MrpResultPage'
import { MrpRunsPage } from './pages/MrpRunsPage'
import { ProductionControlPage } from './pages/ProductionControlPage'
import { ProductionPlanQuarterPage } from './pages/ProductionPlanQuarterPage'
import { ProductionReportWeekPage } from './pages/ProductionReportWeekPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { SpecificationPage } from './pages/SpecificationPage'
import { StageDistributionPage } from './pages/StageDistributionPage'
import { SyncPage } from './pages/SyncPage'

type SectionId = 'home' | 'production-control' | 'production-plan-quarter' | 'production-report-week' | 'mrp-runs' | 'mrp-result' | 'resources' | 'stage-distribution' | 'specification' | 'sync'

const sections: Array<{ id: SectionId; title: string }> = [
  { id: 'home', title: 'Главная' },
  { id: 'production-control', title: 'Журнал заказов' },
  { id: 'production-plan-quarter', title: 'План выпуска' },
  { id: 'production-report-week', title: 'Выпуск недельный' },
  { id: 'mrp-runs', title: 'MRP прогоны' },
  { id: 'resources', title: 'Ресурсы' },
  { id: 'stage-distribution', title: 'Распределение этапов' },
  { id: 'specification', title: 'Спецификации' },
  { id: 'sync', title: 'Синхронизация' },
]

export function App() {
  const [section, setSection] = useState<SectionId>('home')
  const [activeRun, setActiveRun] = useState<PlanningRunRow | null>(null)

  function openRun(run: PlanningRunRow) {
    setActiveRun(run)
    setSection('mrp-result')
  }

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
        {sections.map((item) => (
          <button
            key={item.id}
            className={`navItem ${section === item.id ? 'active' : ''}`}
            onClick={() => setSection(item.id)}
          >
            {item.title}
          </button>
        ))}
      </aside>

      {section === 'home' && <HomePage onNavigate={setSection} />}
      {section === 'production-control' && <ProductionControlPage />}
      {section === 'production-plan-quarter' && <ProductionPlanQuarterPage />}
      {section === 'production-report-week' && <ProductionReportWeekPage />}
      {section === 'mrp-runs' && <MrpRunsPage onOpenRun={openRun} />}
      {section === 'mrp-result' && activeRun && <MrpResultPage runId={activeRun.run_id} onBack={() => setSection('mrp-runs')} />}
      {section === 'resources' && <ResourcesPage />}
      {section === 'stage-distribution' && <StageDistributionPage />}
      {section === 'specification' && <SpecificationPage />}
      {section === 'sync' && <SyncPage />}
    </div>
  )
}
import { HomePage } from './pages/HomePage'
