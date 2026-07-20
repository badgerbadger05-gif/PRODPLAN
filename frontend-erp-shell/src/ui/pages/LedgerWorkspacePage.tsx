import { useEffect, useMemo, useState } from 'react'
import { dateTimeRu, qty } from '../../lib/format'
import {
  mockLedgerDataProvider,
  type LedgerDataProvider,
  type LedgerPostingDetailView,
  type LedgerPostingFilters,
  type LedgerWorkspaceSnapshot,
} from '../../services/ledger'
import {
  LedgerPostingTable,
  ProvenanceTimeline,
  ReconciliationIssuesTable,
  type LedgerPostingView,
} from '../ledger'
import { DocumentWindow } from '../layout/DocumentWindow'

const initialFilters: LedgerPostingFilters = { search: '', eventType: '', direction: '' }

export function LedgerWorkspacePage({ provider = mockLedgerDataProvider }: { provider?: LedgerDataProvider }) {
  const [filters, setFilters] = useState(initialFilters)
  const [appliedFilters, setAppliedFilters] = useState(initialFilters)
  const [snapshot, setSnapshot] = useState<LedgerWorkspaceSnapshot | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<LedgerPostingDetailView | null>(null)
  const [tab, setTab] = useState<'postings' | 'reconciliation'>('postings')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    void provider.loadSnapshot(appliedFilters, controller.signal)
      .then((next) => {
        setSnapshot(next)
        setActiveId((current) => next.postings.some((row) => row.id === current) ? current : next.postings[0]?.id ?? null)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [appliedFilters, provider])

  useEffect(() => {
    if (!activeId) {
      setDetail(null)
      return
    }
    const controller = new AbortController()
    void provider.loadPosting(activeId, controller.signal)
      .then(setDetail)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => controller.abort()
  }, [activeId, provider])

  const eventTypes = useMemo(
    () => [...new Set(snapshot?.postings.map((row) => row.eventType) ?? [])],
    [snapshot?.postings],
  )
  const activate = (posting: LedgerPostingView) => setActiveId(posting.id)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Ledger / Контроль движения</div>
        <div className="runBadge">mock contract · только чтение</div>
      </div>
      <DocumentWindow
        title="Производственный ledger"
        subtitle="Неизменяемые проводки, происхождение расчёта, сторно и сверка проекций"
        hotkeys="Enter Карточка · F5 Обновить"
        footer={(
          <div className="ledgerStatus" aria-live="polite">
            {loading ? 'Загрузка...' : `Проводок: ${snapshot?.postings.length ?? 0}`}
            <span>Срез: {dateTimeRu(snapshot?.calculatedAt) || '—'}</span>
          </div>
        )}
      >
        <div className="toolbar ledgerToolbar">
          <button className={tab === 'postings' ? 'primary' : ''} onClick={() => setTab('postings')}>Проводки</button>
          <button className={tab === 'reconciliation' ? 'primary' : ''} onClick={() => setTab('reconciliation')}>
            Сверка {snapshot?.issues.length ? `(${snapshot.issues.length})` : ''}
          </button>
          <span className="toolbarSpacer" />
          <button onClick={() => setAppliedFilters({ ...filters })}>Обновить</button>
        </div>
        {tab === 'postings' && (
          <>
            <form
              className="ledgerFilters"
              onSubmit={(event) => {
                event.preventDefault()
                setAppliedFilters({ ...filters })
              }}
            >
              <label>
                Поиск
                <input
                  aria-label="Поиск проводок"
                  value={filters.search}
                  placeholder="Проводка, документ, пул..."
                  onChange={(event) => setFilters({ ...filters, search: event.target.value })}
                />
              </label>
              <label>
                Событие
                <select value={filters.eventType} onChange={(event) => setFilters({ ...filters, eventType: event.target.value })}>
                  <option value="">Все</option>
                  {eventTypes.map((eventType) => <option key={eventType}>{eventType}</option>)}
                </select>
              </label>
              <label>
                Направление
                <select
                  value={filters.direction}
                  onChange={(event) => setFilters({ ...filters, direction: event.target.value as LedgerPostingFilters['direction'] })}
                >
                  <option value="">Все</option>
                  <option value="receipt">Приход</option>
                  <option value="issue">Расход</option>
                </select>
              </label>
              <button type="submit">Найти</button>
            </form>
            {error && <div className="errorLine" role="alert">{error}</div>}
            <div className="ledgerSplit">
              <div className="tablePane">
                <LedgerPostingTable rows={snapshot?.postings ?? []} activeId={activeId} onActivate={activate} />
              </div>
              <aside className="detailPane ledgerDetail">
                {!detail && <div className="emptyDetail">Выберите проводку</div>}
                {detail && (
                  <>
                    <div className="detailTitle">{detail.posting.id}</div>
                    <div className="detailMeta">{detail.posting.eventType} · {detail.posting.sourceDocument}</div>
                    <div className="detailGrid">
                      <span>Пул</span><strong>{detail.balance.poolLabel}</strong>
                      <span>Остаток</span><strong>{qty(detail.balance.quantity)} {detail.balance.unit}</strong>
                      <span>Рассчитан</span><strong>{dateTimeRu(detail.balance.calculatedAt)}</strong>
                      <span>Correlation ID</span><code>{detail.posting.correlationId || '—'}</code>
                    </div>
                    <h2>Происхождение</h2>
                    <ProvenanceTimeline steps={detail.provenance} />
                    <h2>Цепочка сторно</h2>
                    <LedgerPostingTable rows={detail.reversalChain} activeId={detail.posting.id} onActivate={activate} />
                  </>
                )}
              </aside>
            </div>
          </>
        )}
        {tab === 'reconciliation' && (
          <div className="tablePane">
            <div className="hintLine">Сверка выполняется на всю каноническую область. Проекции доступны только для чтения.</div>
            <ReconciliationIssuesTable rows={snapshot?.issues ?? []} />
          </div>
        )}
      </DocumentWindow>
    </main>
  )
}
