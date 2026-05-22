import { useEffect, useState } from 'react'
import type { PlanningRunRow } from '../../domain/planning'
import { dateTimeRu, qty } from '../../lib/format'
import { listPlanningRuns } from '../../services/planning'
import { listResources } from '../../services/resources'
import { listWarehouses } from '../../services/sync'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

type SectionId =
  | 'production-control'
  | 'production-plan-quarter'
  | 'production-report-week'
  | 'mrp-runs'
  | 'resources'
  | 'stage-distribution'
  | 'specification'
  | 'sync'

type Props = {
  onNavigate: (section: SectionId) => void
}

export function HomePage({ onNavigate }: Props) {
  const [latestRun, setLatestRun] = useState<PlanningRunRow | null>(null)
  const [resourceCount, setResourceCount] = useState(0)
  const [warehouseTotal, setWarehouseTotal] = useState(0)
  const [warehouseSelected, setWarehouseSelected] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    setLoading(true)
    setError('')
    try {
      const [runs, resources, warehouses] = await Promise.all([
        listPlanningRuns({ limit: 1, offset: 0 }),
        listResources(),
        listWarehouses(),
      ])
      setLatestRun(runs.rows?.[0] ?? null)
      setResourceCount(resources.length)
      setWarehouseTotal(warehouses.total ?? 0)
      setWarehouseSelected(warehouses.selected_total ?? 0)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">PRODPLAN / Рабочая панель</div>
        <div className="runBadge">ERP shell</div>
      </div>

      <DocumentWindow
        title="Главная"
        subtitle="Оперативный вход в рабочие разделы PRODPLAN без декоративных заглушек"
        hotkeys="Только реальные разделы и живые показатели"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={1}
            visibleTo={4}
            total={4}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        {error && <div className="errorLine">{error}</div>}
        <div className="homeGrid">
          <section className="homeMetrics">
            <Metric title="Последний MRP" value={latestRun ? `#${latestRun.run_id}` : '—'} hint={latestRun ? `${latestRun.status} · ${dateTimeRu(latestRun.started_at)}` : 'прогонов нет'} />
            <Metric title="Производство" value={qty(latestRun?.order_count)} hint="заказов в последнем прогоне" />
            <Metric title="Закупки" value={qty(latestRun?.purchase_count)} hint="строк потребности" />
            <Metric title="Склады" value={`${warehouseSelected}/${warehouseTotal}`} hint="выбрано для остатков" />
            <Metric title="Ресурсы" value={qty(resourceCount)} hint="производственных участков" />
          </section>

          <section className="homePanel">
            <h2>Рабочие разделы</h2>
            <div className="homeActions">
              <button className="primary" onClick={() => onNavigate('production-control')}>Журнал заказов</button>
              <button onClick={() => onNavigate('production-plan-quarter')}>План выпуска</button>
              <button onClick={() => onNavigate('production-report-week')}>Выпуск недельный</button>
              <button onClick={() => onNavigate('mrp-runs')}>MRP прогоны</button>
              <button onClick={() => onNavigate('sync')}>Синхронизация</button>
              <button onClick={() => onNavigate('resources')}>Ресурсы</button>
              <button onClick={() => onNavigate('stage-distribution')}>Распределение этапов</button>
              <button onClick={() => onNavigate('specification')}>Спецификации</button>
            </div>
          </section>

          <section className="homePanel">
            <h2>Что перенесено</h2>
            <table className="miniStatusTable">
              <tbody>
                <tr><td>План выпуска</td><td><span className="pill ready">работает</span></td></tr>
                <tr><td>Отчёт выпуска</td><td><span className="pill ready">работает</span></td></tr>
                <tr><td>MRP</td><td><span className="pill ready">работает</span></td></tr>
                <tr><td>Синхронизация</td><td><span className="pill ready">работает</span></td></tr>
                <tr><td>Ресурсы</td><td><span className="pill partial">просмотр</span></td></tr>
              </tbody>
            </table>
          </section>
        </div>
      </DocumentWindow>
    </main>
  )
}

function Metric({ title, value, hint }: { title: string; value: string; hint: string }) {
  return (
    <div className="metricCell">
      <span>{title}</span>
      <strong>{value}</strong>
      <em>{hint}</em>
    </div>
  )
}
