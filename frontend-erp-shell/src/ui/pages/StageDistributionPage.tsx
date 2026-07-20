import { useMemo, useState } from 'react'
import type { ResourceDistributionResult } from '../../domain/stageDistribution'
import { dateTimeRu } from '../../lib/format'
import { calculateResourceDistribution } from '../../services/stageDistribution'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import {
  ResourceTabs,
  StageDistributionControls,
  StageDistributionTable,
} from './stage-distribution/components'
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
        <StageDistributionControls
          aggregate={aggregate}
          loading={loading}
          onCalculate={() => void calculate()}
          onAggregateChange={setAggregate}
        />

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <ResourceTabs
          resources={resources}
          activeResourceId={active?.resource_id ?? null}
          onActivate={setActiveId}
        />

        <StageDistributionTable
          components={components}
          hasResources={resources.length > 0}
          loading={loading}
        />
      </DocumentWindow>
    </main>
  )
}
