import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { dateTimeRu, qty } from '../../lib/format'
import {
  mockLedgerDataProvider,
  type LedgerDataProvider,
  type LedgerPostingDetailView,
  type LedgerPostingFilters,
  type LedgerWorkspaceSnapshot,
} from '../../services/ledger'
import {
  AuditTimeline,
  LedgerPostingTable,
  ProvenanceTimeline,
  ReconciliationIssuesTable,
  type LedgerPostingView,
} from '../ledger'
import { DocumentWindow } from '../layout/DocumentWindow'
import { Button } from '../kit'

const initialFilters: LedgerPostingFilters = { search: '', eventType: '', direction: '' }

type Props = {
  provider?: LedgerDataProvider
  initialPostingId?: string | null
  onActiveIdChange?: (id: string) => void
}

export function LedgerWorkspacePage({
  provider = mockLedgerDataProvider,
  initialPostingId = null,
  onActiveIdChange,
}: Props) {
  const [filters, setFilters] = useState(initialFilters)
  const [appliedFilters, setAppliedFilters] = useState(initialFilters)
  const [snapshot, setSnapshot] = useState<LedgerWorkspaceSnapshot | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<LedgerPostingDetailView | null>(null)
  const [tab, setTab] = useState<'postings' | 'reconciliation'>('postings')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const snapshotSequence = useRef(0)
  const detailSequence = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    const sequence = ++snapshotSequence.current
    setLoading(true)
    setError('')
    void provider.loadSnapshot(appliedFilters, controller.signal)
      .then((next) => {
        if (sequence !== snapshotSequence.current) return
        setSnapshot(next)
        setActiveId((current) => {
          const preferred = initialPostingId && next.postings.some((row) => row.id === initialPostingId)
            ? initialPostingId
            : current
          return next.postings.some((row) => row.id === preferred) ? preferred : next.postings[0]?.id ?? null
        })
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [appliedFilters, initialPostingId, provider])

  useEffect(() => {
    if (!activeId) {
      setDetail(null)
      return
    }
    const controller = new AbortController()
    const sequence = ++detailSequence.current
    setDetail(null)
    void provider.loadPosting(activeId, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted && sequence === detailSequence.current) setDetail(next)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => controller.abort()
  }, [activeId, provider])

  const eventTypes = useMemo(
    () => [...new Set(snapshot?.postings.map((row) => row.eventType) ?? [])],
    [snapshot?.postings],
  )
  const activate = (posting: LedgerPostingView) => {
    setActiveId(posting.id)
    onActiveIdChange?.(posting.id)
  }

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
          <Button variant={tab === 'postings' ? 'primary' : 'default'} onClick={() => setTab('postings')}>Проводки</Button>
          <Button variant={tab === 'reconciliation' ? 'primary' : 'default'} onClick={() => setTab('reconciliation')}>
            Сверка {snapshot?.issues.length ? `(${snapshot.issues.length})` : ''}
          </Button>
          <span className="toolbarSpacer" />
          <Button onClick={() => setAppliedFilters({ ...filters })}>Обновить</Button>
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
              <Button type="submit">Найти</Button>
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
                    <h2>Аудит</h2>
                    <AuditTimeline events={detail.auditTrail} />
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

export function LedgerWorkspaceRoute() {
  const { postingId } = useParams()
  const navigate = useNavigate()
  return (
    <LedgerWorkspacePage
      initialPostingId={postingId}
      onActiveIdChange={(id) => navigate(`/ledger/postings/${encodeURIComponent(id)}`)}
    />
  )
}
