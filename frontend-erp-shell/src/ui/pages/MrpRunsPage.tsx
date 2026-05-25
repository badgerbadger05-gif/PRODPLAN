import { useCallback, useEffect, useMemo, useState } from 'react'
import { planningStatusLabel, type PlanningRunRow } from '../../domain/planning'
import { dateTimeRu, qty } from '../../lib/format'
import { listPlanningRuns, startPlanningRun } from '../../services/planning'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const limit = 30
const horizonOptions = [30, 60, 90, 120]

type Props = {
  onOpenRun: (run: PlanningRunRow) => void
}

export function MrpRunsPage({ onOpenRun }: Props) {
  const [rows, setRows] = useState<PlanningRunRow[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [horizonDays, setHorizonDays] = useState(90)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [calculating, setCalculating] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

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

  async function calculate() {
    setCalculating(true)
    setError('')
    setMessage('')
    try {
      const result = await startPlanningRun({ horizon_days: horizonDays, started_by: 'erp-shell' })
      setMessage(`Создан прогон #${result.run_id}`)
      await load(0)
      setActiveId(result.run_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCalculating(false)
    }
  }

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
        subtitle="Контрольные прогоны расчёта: производство, закупки, перегрузы и горизонт"
        hotkeys="F5 Обновить · Enter Детали"
        footer={(
          <StatusBar
            loading={loading || calculating}
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
        <div className="commandBar">
          <button className="primary" onClick={() => void calculate()} disabled={calculating || loading}>Рассчитать</button>
          <button onClick={() => void load(offset)} disabled={loading || calculating}>Обновить</button>
          <div className="barSeparator" />
          <label className="inlineControl">
            <span>Горизонт</span>
            <select value={horizonDays} onChange={(e) => setHorizonDays(Number(e.target.value))} disabled={calculating}>
              {horizonOptions.map((value) => <option key={value} value={value}>{value} дней</option>)}
            </select>
          </label>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split splitRuns">
          <div className="tablePane">
            <table className="journalTable runsTable">
              <thead>
                <tr>
                  <th>RUN</th>
                  <th>Статус</th>
                  <th>Старт</th>
                  <th>Финиш</th>
                  <th>Горизонт</th>
                  <th>Производство</th>
                  <th>Закупки</th>
                  <th>Перегрузы</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.run_id} className={row.run_id === activeRun?.run_id ? 'activeRow' : ''} onClick={() => setActiveId(row.run_id)} onDoubleClick={() => onOpenRun(row)}>
                    <td className="orderCell">
                      <strong>#{row.run_id}</strong>
                      <span>расчёт</span>
                    </td>
                    <td><span className={`pill ${row.status.toLowerCase()}`}>{planningStatusLabel(row.status)}</span></td>
                    <td>{dateTimeRu(row.started_at) || '—'}</td>
                    <td>{dateTimeRu(row.finished_at) || '—'}</td>
                    <td className="numCell"><strong>{qty(row.horizon_days)}</strong><span>дн.</span></td>
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
                  <span>Горизонт</span><strong>{qty(activeRun.horizon_days)} дн.</strong>
                  <span>Производство</span><strong>{qty(activeRun.order_count)}</strong>
                  <span>Закупки</span><strong>{qty(activeRun.purchase_count)}</strong>
                  <span>Перегрузы</span><strong>{qty(activeRun.overload_buckets)}</strong>
                </div>
                <div className="detailActions">
                  <button className="primary" onClick={() => onOpenRun(activeRun)}>Открыть результат</button>
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
