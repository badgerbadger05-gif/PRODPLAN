import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { PlanningRunRow } from '../../domain/planning'
import { dateTimeRu, qty } from '../../lib/format'
import { listPlanningRuns } from '../../services/planning'
import { listResources } from '../../services/resources'
import { listWarehouses } from '../../services/sync'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { canAccessResource, frontendResources } from '../resourceRegistry'
import { mockUser } from '../session'
import { useOptionalSession } from '../session'

export function HomePage() {
  const navigate = useNavigate()
  const session = useOptionalSession()
  const user = session?.user ?? mockUser()
  const available = new Set(
    frontendResources
      .filter((resource) => canAccessResource(resource, user.roles))
      .map((resource) => resource.name),
  )
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
              {available.has('period_plan') && <button className="primary" onClick={() => navigate('/period-plan')}>Планирование выпуска</button>}
              {available.has('production_order') && <button onClick={() => navigate('/production-control')}>Журнал заказов</button>}
              {available.has('plan_run') && <button onClick={() => navigate('/mrp-runs')}>MRP прогоны</button>}
              {available.has('ledger') && <button onClick={() => navigate('/ledger')}>Ledger</button>}
              {available.has('sync') && <button onClick={() => navigate('/sync')}>Синхронизация</button>}
              {available.has('resources') && <button onClick={() => navigate('/resources')}>Ресурсы</button>}
              {available.has('specification') && <button onClick={() => navigate('/specification')}>Спецификации</button>}
            </div>
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
