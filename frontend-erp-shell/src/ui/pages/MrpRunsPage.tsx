import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { planningStatusLabel, type PlanningRunRow } from '../../domain/planning'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import { listPlanningRuns } from '../../services/planning'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const limit = 30

function planLabel(row?: PlanningRunRow | null) {
  if (!row) return '—'
  if (row.source_plan_name) return row.source_plan_name
  if (row.source_plan_id) return `План #${row.source_plan_id}`
  return 'скользящий план'
}

function periodLabel(row?: PlanningRunRow | null) {
  if (!row) return '—'
  const from = dateRu(row.period_from)
  const to = dateRu(row.period_to)
  if (from && to) return `${from} — ${to}`
  if (from || to) return from || to
  return '—'
}

export function MrpRunsPage() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<PlanningRunRow[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const activeRun = useMemo(() => rows.find((row) => row.run_id === activeId) ?? rows[0] ?? null, [rows, activeId])

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    try {
      const data = await listPlanningRuns({ limit, offset: nextOffset })
      setRows(data.rows ?? [])
      setTotal(data.total ?? 0)
      setOffset(nextOffset)
      setActiveId((current) => {
        if (current && data.rows?.some((row) => row.run_id === current)) return current
        return data.rows?.[0]?.run_id ?? null
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(0)
  }, [load])

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">MRP / Прогоны расчёта потребностей</div>
        <div className="runBadge">Всего: {total}</div>
      </div>

      <DocumentWindow
        title="MRP планирование"
        subtitle="Контрольные прогоны расчёта: производство, закупки и перегрузы"
        hotkeys="Enter Детали"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={activeRun ? 1 : 0}
            canPrev={offset > 0}
            canNext={offset + rows.length < total}
            onPrev={() => void load(Math.max(0, offset - limit))}
            onNext={() => void load(offset + limit)}
          />
        )}
      >
        {error && <div className="errorLine">{error}</div>}

        <div className="split splitRuns">
          <div className="tablePane">
            <table className="journalTable runsTable">
              <thead>
                <tr>
                  <th>RUN</th>
                  <th>План</th>
                  <th>Статус</th>
                  <th>Период</th>
                  <th>Производство</th>
                  <th>Закупки</th>
                  <th>Перегрузы</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.run_id} className={row.run_id === activeRun?.run_id ? 'activeRow' : ''} onClick={() => setActiveId(row.run_id)} onDoubleClick={() => navigate(`/mrp-runs/${row.run_id}`)}>
                    <td className="orderCell">
                      <strong>#{row.run_id}</strong>
                      <span>расчёт</span>
                    </td>
                    <td className="orderCell">
                      <strong>{planLabel(row)}</strong>
                      {row.source_plan_id && <span>план #{row.source_plan_id}</span>}
                    </td>
                    <td><span className={`pill ${row.status.toLowerCase()}`}>{planningStatusLabel(row.status)}</span></td>
                    <td>{periodLabel(row)}</td>
                    <td className="numCell"><strong>{qty(row.order_count)}</strong><span>заказов</span></td>
                    <td className="numCell"><strong>{qty(row.purchase_count)}</strong><span>строк</span></td>
                    <td className="numCell"><strong>{qty(row.overload_buckets)}</strong><span>окон</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <aside className="detailPane">
            <h2>Сводка прогона</h2>
            {activeRun ? (
              <>
                <div className="detailTitle">Прогон #{activeRun.run_id}</div>
                <div className="detailMeta">{planningStatusLabel(activeRun.status)}</div>
                <div className="detailGrid">
                  <span>Старт</span><strong>{dateTimeRu(activeRun.started_at) || '—'}</strong>
                  <span>Финиш</span><strong>{dateTimeRu(activeRun.finished_at) || '—'}</strong>
                  <span>Период</span><strong>{periodLabel(activeRun)}</strong>
                  <span>План</span><strong>{planLabel(activeRun)}</strong>
                  <span>Потребность</span><strong>{qty(activeRun.requirement_count)} / {qty(activeRun.requirement_remaining_qty)}</strong>
                  <span>Производство</span><strong>{qty(activeRun.order_count)}</strong>
                  <span>Закупки</span><strong>{qty(activeRun.purchase_count)}</strong>
                  <span>Перегрузы</span><strong>{qty(activeRun.overload_buckets)}</strong>
                </div>
                <div className="detailActions">
                  <button className="primary" onClick={() => navigate(`/mrp-runs/${activeRun.run_id}`)}>Открыть результат</button>
                </div>
              </>
            ) : (
              <div className="emptyDetail">Прогоны не загружены</div>
            )}
          </aside>
        </div>
      </DocumentWindow>
    </main>
  )
}
