import { useMemo, useState } from 'react'
import type { ResourceDistributionResult } from '../../domain/stageDistribution'
import { dateTimeRu, qty } from '../../lib/format'
import { calculateResourceDistribution } from '../../services/stageDistribution'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { aggregateComponents, flattenResourceComponents } from './stage-distribution/model'

export function StageDistributionPage() {
  const [resources, setResources] = useState<ResourceDistributionResult[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [asOf, setAsOf] = useState<string | null>(null)
  const [aggregate, setAggregate] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const active = useMemo(() => resources.find((row) => row.resource_id === activeId) ?? resources[0] ?? null, [resources, activeId])
  const components = useMemo(() => {
    const rows = flattenResourceComponents(active)
    return aggregate ? aggregateComponents(rows) : rows
  }, [active, aggregate])

  async function calculate() {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await calculateResourceDistribution()
      setResources(data.resources ?? [])
      setAsOf(data.asOf ?? null)
      setActiveId((current) => current && data.resources?.some((r) => r.resource_id === current) ? current : data.resources?.[0]?.resource_id ?? null)
      setMessage('Распределение рассчитано')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Распределение этапов по участкам</div>
        <div className="runBadge">Остатки: {dateTimeRu(asOf) || '—'}</div>
      </div>

      <DocumentWindow
        title="Распределение этапов"
        subtitle="Расчёт деталей по производственным участкам на основе спецификаций и видов производства"
        hotkeys="Рабочий endpoint: /resources/calculate_distribution"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={components.length ? 1 : 0}
            visibleTo={components.length}
            total={components.length}
            selectedCount={resources.length}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button className="primary" onClick={() => void calculate()} disabled={loading}>Рассчитать</button>
          <label className="inlineControl">
            <input type="checkbox" checked={aggregate} onChange={(e) => setAggregate(e.target.checked)} />
            <span>Суммировать одинаковые детали</span>
          </label>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="tabsBar">
          {resources.map((resource) => (
            <button key={resource.resource_id} className={resource.resource_id === active?.resource_id ? 'activeTab' : ''} onClick={() => setActiveId(resource.resource_id)}>
              {resource.resource_name} · {qty(resource.norm_hours)} н/ч
            </button>
          ))}
        </div>

        <div className="tablePane resultTablePane">
          <table className="journalTable stageDistributionTable">
            <thead>
              <tr>
                <th>Деталь</th>
                <th>Артикул</th>
                <th>Этап</th>
                <th>Кол-во</th>
                <th>Остаток</th>
                <th>Норма</th>
                <th>Сумма н/ч</th>
              </tr>
            </thead>
            <tbody>
              {components.map((row, index) => (
                <tr key={`${row.item_id}-${row.stage_id ?? 'x'}-${index}`}>
                  <td className="itemCell">
                    <strong>{row.item_name}</strong>
                    <span>{row.item_code}</span>
                  </td>
                  <td>{row.item_article || ''}</td>
                  <td>{row.stage_name || '—'}</td>
                  <td className="numCell"><strong>{qty(row.qty_per_unit)}</strong></td>
                  <td className="numCell"><strong>{qty(row.stock_qty)}</strong></td>
                  <td className="numCell"><strong>{qty(row.norm_hours)}</strong></td>
                  <td className="numCell"><strong>{qty(row.norm_hours_total)}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!resources.length && !loading && <div className="emptyDetail">Нажмите «Рассчитать», чтобы получить распределение</div>}
        </div>
      </DocumentWindow>
    </main>
  )
}
