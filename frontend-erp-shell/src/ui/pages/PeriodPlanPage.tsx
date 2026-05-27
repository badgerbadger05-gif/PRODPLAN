import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import type {
  ExecutionJournalResponse,
  PeriodPlan,
  PeriodPlanMatrix,
  PeriodPlanRun,
} from '../../domain/planning'
import {
  coverageClass,
  flowClass,
  flowLabel,
  periodPlanStatusClass,
  periodPlanStatusLabel,
  planningStatusLabel,
} from '../../domain/planning'
import type { NomenclatureSearchItem } from '../../domain/productionPlan'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import { ensurePlanItem, searchNomenclature } from '../../services/productionPlan'
import {
  addItemToPeriodPlan,
  allocatePurchases,
  allocateRework,
  archivePeriodPlan,
  bulkUpsertPeriodPlanLines,
  createMrpSnapshot,
  createPeriodPlan,
  createProductionOrdersFromRequirements,
  deleteItemFromPeriodPlan,
  deletePeriodPlan,
  fixPeriodPlan,
  getExecutionJournal,
  getPeriodPlanMatrix,
  listPeriodPlanRuns,
  listPeriodPlans,
  unarchivePeriodPlan,
  updatePeriodPlanHeader,
} from '../../services/periodPlan'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const PLAN_LIMIT = 50

type Tab = 'matrix' | 'journal'

function nextFriday(offset = 0) {
  const d = new Date()
  const dow = d.getDay()
  d.setDate(d.getDate() + ((5 - dow + 7) % 7) + offset * 7)
  return d.toISOString().slice(0, 10)
}

function bucketLabel(iso: string) {
  return dateRu(iso).slice(0, 5)
}

type ForecastInfo = {
  forecast_date?: string | null
  forecast_shift_days?: number | null
  forecast_reason?: string | null
}

function ForecastShift({ forecast }: { forecast?: ForecastInfo | null }) {
  if (!forecast || forecast.forecast_shift_days === null || forecast.forecast_shift_days === undefined) return null
  const days = Number(forecast.forecast_shift_days)
  if (!Number.isFinite(days) || days === 0) return null
  const cls = days > 5 ? 'late' : days > 0 ? 'warn' : 'early'
  const label = `${days > 0 ? '+' : ''}${days} дн`
  const dateText = forecast.forecast_date ? dateRu(forecast.forecast_date).slice(0, 5) : ''
  const title = [forecast.forecast_reason, forecast.forecast_date ? `прогноз ${dateRu(forecast.forecast_date)}` : null].filter(Boolean).join(' · ')
  return <span className={`forecastShift ${cls}`} title={title}>{label}{dateText ? ` · ${dateText}` : ''}</span>
}

// ── Main page (list ↔ detail) ────────────────────────────────────────────────

export function PeriodPlanPage() {
  const { planId: planIdParam } = useParams<{ planId: string }>()
  const navigate = useNavigate()
  const planId = planIdParam ? Number(planIdParam) : null

  if (planId !== null) {
    return (
      <PeriodPlanDetailView
        planId={planId}
        onBack={() => navigate('/period-plan')}
      />
    )
  }

  return (
    <PeriodPlanListView
      onOpenPlan={(id) => navigate(`/period-plan/${id}`)}
    />
  )
}

// ── List view ────────────────────────────────────────────────────────────────

interface ListViewProps {
  onOpenPlan: (id: number) => void
}

type SortKey = 'name' | 'status' | 'period_from' | 'period_to' | 'fixed_at' | 'created_at'
type SortDir = 'asc' | 'desc'

function PeriodPlanListView({ onOpenPlan }: ListViewProps) {
  const [plans, setPlans] = useState<PeriodPlan[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)

  // Filters
  const [filterStatus, setFilterStatus] = useState<string>('')
  const [filterFrom, setFilterFrom] = useState<string>('')
  const [filterTo, setFilterTo] = useState<string>('')
  const [filterCreatedBy, setFilterCreatedBy] = useState<string>('')
  const [sortBy, setSortBy] = useState<SortKey>('period_from')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newFrom, setNewFrom] = useState(nextFriday(0))
  const [newTo, setNewTo] = useState(nextFriday(3))
  const [newComment, setNewComment] = useState('')
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const selected = plans.find((p) => p.id === selectedId) ?? null
  const canDelete = selected?.status !== 'archived' // backend will reject if there are SUCCESS MRP runs
  const dateOrderInvalid = !!(newFrom && newTo && newFrom > newTo)

  const loadList = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    try {
      const data = await listPeriodPlans({
        limit: PLAN_LIMIT,
        offset: nextOffset,
        status: filterStatus || undefined,
        period_from: filterFrom || undefined,
        period_to: filterTo || undefined,
        created_by: filterCreatedBy || undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
      })
      setPlans(data.rows ?? [])
      setTotal(data.total ?? 0)
      setOffset(nextOffset)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [filterStatus, filterFrom, filterTo, filterCreatedBy, sortBy, sortDir])

  useEffect(() => { void loadList(0) }, [loadList])

  // Keyboard hotkeys: F5 refresh, Enter open
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null
      const inInput = !!target && /^(INPUT|TEXTAREA|SELECT)$/i.test(target.tagName)
      if (e.key === 'F5') {
        e.preventDefault()
        void loadList(offset)
        return
      }
      if (e.key === 'Enter' && !inInput && selected) {
        e.preventDefault()
        onOpenPlan(selected.id)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [loadList, offset, selected, onOpenPlan])

  async function handleCreate() {
    if (!newName.trim() || !newFrom || !newTo) return
    if (dateOrderInvalid) {
      setError('Дата окончания периода не может быть раньше даты начала')
      return
    }
    setCreating(true)
    setError('')
    try {
      const created = await createPeriodPlan({
        name: newName.trim(),
        period_from: newFrom,
        period_to: newTo,
        comment: newComment.trim() || null,
      })
      setShowCreate(false)
      setNewName('')
      setNewComment('')
      await loadList(0)
      if (created?.id) onOpenPlan(created.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete() {
    if (!selected) return
    if (!confirm(`Удалить план «${selected.name}»?`)) return
    setDeleting(true)
    setError('')
    setMessage('')
    try {
      await deletePeriodPlan(selected.id)
      setMessage(`План «${selected.name}» удалён`)
      setSelectedId(null)
      await loadList(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  function toggleSort(key: SortKey) {
    if (sortBy === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortBy(key)
      setSortDir('desc')
    }
  }

  function sortArrow(key: SortKey) {
    if (sortBy !== key) return ''
    return sortDir === 'asc' ? ' ▲' : ' ▼'
  }

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + plans.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование / Планирование выпуска</div>
        <div className="runBadge">Планов: {total}</div>
      </div>

      <DocumentWindow
        title="Планирование выпуска"
        subtitle="Список планов производства на выбранный период"
        hotkeys="F5 Обновить · Enter Открыть"
        footer={(
          <StatusBar
            loading={loading || deleting}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={selected ? 1 : 0}
            canPrev={offset > 0}
            canNext={offset + plans.length < total}
            onPrev={() => void loadList(Math.max(0, offset - PLAN_LIMIT))}
            onNext={() => void loadList(offset + PLAN_LIMIT)}
          />
        )}
      >
        <div className="commandBar">
          <button className="primary" onClick={() => setShowCreate(true)} disabled={creating}>Новый план</button>
          <button onClick={() => void loadList(offset)} disabled={loading}>Обновить</button>
          <div className="barSeparator" />
          <button onClick={() => selected && onOpenPlan(selected.id)} disabled={!selected}>Открыть</button>
          {canDelete && (
            <button onClick={() => void handleDelete()} disabled={deleting || !selected} style={{ color: 'var(--red)' }}>
              Удалить
            </button>
          )}
        </div>

        {/* Filter bar */}
        <div className="requisites" style={{ gridTemplateColumns: '160px 150px 150px minmax(180px,1fr) auto' }}>
          <label>
            <span>Статус</span>
            <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
              <option value="">Все</option>
              <option value="draft">Черновик</option>
              <option value="fixed">Зафиксирован</option>
              <option value="archived">Архив</option>
            </select>
          </label>
          <label>
            <span>Период с</span>
            <input type="date" value={filterFrom} onChange={(e) => setFilterFrom(e.target.value)} />
          </label>
          <label>
            <span>Период по</span>
            <input type="date" value={filterTo} onChange={(e) => setFilterTo(e.target.value)} />
          </label>
          <label>
            <span>Автор (created_by)</span>
            <input value={filterCreatedBy} onChange={(e) => setFilterCreatedBy(e.target.value)} placeholder="любая часть имени" />
          </label>
          <button
            style={{ alignSelf: 'end' }}
            onClick={() => { setFilterStatus(''); setFilterFrom(''); setFilterTo(''); setFilterCreatedBy('') }}
          >
            Сбросить
          </button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        {showCreate && (
          <div className="requisites" style={{ gridTemplateColumns: 'minmax(220px,1fr) 150px 150px minmax(220px,1fr) auto auto' }}>
            <label>
              <span>Название плана</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Например: МАЙ 2026" autoFocus />
            </label>
            <label>
              <span>Период с (пятница)</span>
              <input type="date" value={newFrom} onChange={(e) => setNewFrom(e.target.value)} />
            </label>
            <label>
              <span>Период по</span>
              <input type="date" value={newTo} onChange={(e) => setNewTo(e.target.value)} style={dateOrderInvalid ? { borderColor: 'var(--red)' } : undefined} />
            </label>
            <label>
              <span>Комментарий</span>
              <input value={newComment} onChange={(e) => setNewComment(e.target.value)} placeholder="опционально" />
            </label>
            <button
              className="primary"
              style={{ alignSelf: 'end' }}
              onClick={() => void handleCreate()}
              disabled={creating || !newName.trim() || dateOrderInvalid}
              title={dateOrderInvalid ? 'Период «по» раньше периода «с»' : undefined}
            >
              Создать
            </button>
            <button style={{ alignSelf: 'end' }} onClick={() => { setShowCreate(false); setError('') }}>Отмена</button>
            {dateOrderInvalid && (
              <div style={{ gridColumn: '1 / -1', color: 'var(--red)', fontSize: 11 }}>
                Дата окончания периода не может быть раньше даты начала. Шаг по неделям — пятницы.
              </div>
            )}
          </div>
        )}

        <div className="tablePane resultTablePane" style={{ flex: 1 }}>
          <table className="journalTable" style={{ minWidth: 980, tableLayout: 'fixed' }}>
            <thead>
              <tr>
                <th style={{ width: 240, cursor: 'pointer' }} onClick={() => toggleSort('name')}>Название{sortArrow('name')}</th>
                <th style={{ width: 110, cursor: 'pointer' }} onClick={() => toggleSort('status')}>Статус{sortArrow('status')}</th>
                <th style={{ width: 180, cursor: 'pointer' }} onClick={() => toggleSort('period_from')}>Период{sortArrow('period_from')}</th>
                <th style={{ width: 140, cursor: 'pointer' }} onClick={() => toggleSort('fixed_at')}>Зафиксирован{sortArrow('fixed_at')}</th>
                <th style={{ width: 110 }}>Кем</th>
                <th style={{ width: 140, cursor: 'pointer' }} onClick={() => toggleSort('created_at')}>Создан{sortArrow('created_at')}</th>
                <th style={{ width: 64, textAlign: 'right' }}>Строк</th>
                <th>Комментарий</th>
              </tr>
            </thead>
            <tbody>
              {plans.map((plan) => (
                <tr
                  key={plan.id}
                  className={plan.id === selectedId ? 'activeRow' : ''}
                  style={{ cursor: 'pointer', opacity: plan.status === 'archived' ? 0.62 : undefined }}
                  onClick={() => setSelectedId(plan.id === selectedId ? null : plan.id)}
                  onDoubleClick={() => onOpenPlan(plan.id)}
                >
                  <td><strong>{plan.name}</strong></td>
                  <td><span className={`pill ${periodPlanStatusClass(plan.status)}`}>{periodPlanStatusLabel(plan.status)}</span></td>
                  <td><span className="muted">{dateRu(plan.period_from)} — {dateRu(plan.period_to)}</span></td>
                  <td><span className="muted">{plan.fixed_at ? dateTimeRu(plan.fixed_at) : '—'}</span></td>
                  <td><span className="muted">{plan.fixed_by ?? plan.created_by ?? '—'}</span></td>
                  <td><span className="muted">{plan.created_at ? dateTimeRu(plan.created_at) : '—'}</span></td>
                  <td style={{ textAlign: 'right' }}><strong>{plan.line_count ?? 0}</strong></td>
                  <td><span className="muted">{plan.comment ?? '—'}</span></td>
                </tr>
              ))}
              {!loading && !plans.length && (
                <tr><td colSpan={8}><div className="emptyDetail">Нет планов</div></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}

// ── Detail view (full plan window) ───────────────────────────────────────────

interface DetailViewProps {
  planId: number
  onBack: () => void
}

type JournalSortKey =
  | 'item_article'
  | 'item_code'
  | 'item_name'
  | 'flow'
  | 'bom_level'
  | 'gross_qty'
  | 'net_qty'
  | 'ordered_qty'
  | 'completed_qty'
  | 'remaining_qty'
  | 'coverage_pct'

function PeriodPlanDetailView({ planId, onBack }: DetailViewProps) {
  const [plan, setPlan] = useState<PeriodPlan | null>(null)
  const [tab, setTab] = useState<Tab>('matrix')

  const [matrix, setMatrix] = useState<PeriodPlanMatrix | null>(null)
  const [matrixLoading, setMatrixLoading] = useState(false)
  const [matrixError, setMatrixError] = useState('')
  const [dirty, setDirty] = useState<Record<number, Record<string, number>>>({})
  const [saving, setSaving] = useState(false)

  const [journal, setJournal] = useState<ExecutionJournalResponse | null>(null)
  const [journalLoading, setJournalLoading] = useState(false)
  const [journalError, setJournalError] = useState('')
  const [journalFlow, setJournalFlow] = useState('')
  const [journalBomLevel, setJournalBomLevel] = useState<string>('')
  const [journalCoverage, setJournalCoverage] = useState<string>('')
  const [journalSortBy, setJournalSortBy] = useState<JournalSortKey>('bom_level')
  const [journalSortDir, setJournalSortDir] = useState<SortDir>('asc')
  const [expandedReq, setExpandedReq] = useState<number | null>(null)
  const [lastRunId, setLastRunId] = useState<number | null>(null)

  const [runs, setRuns] = useState<PeriodPlanRun[]>([])
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)

  const [acting, setActing] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  // Header edit mode (rename/period/comment)
  const [editingHeader, setEditingHeader] = useState(false)
  const [editName, setEditName] = useState('')
  const [editFrom, setEditFrom] = useState('')
  const [editTo, setEditTo] = useState('')
  const [editComment, setEditComment] = useState('')

  const [searchQuery, setSearchQuery] = useState('')
  const [searchRows, setSearchRows] = useState<NomenclatureSearchItem[]>([])
  const [searching, setSearching] = useState(false)
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [searchHighlight, setSearchHighlight] = useState(0)
  const [deletingItemId, setDeletingItemId] = useState<number | null>(null)

  const searchInputRef = useRef<HTMLInputElement | null>(null)
  const matrixInputRefs = useRef<Record<string, HTMLInputElement | null>>({})

  const isDraft = plan?.status === 'draft'
  const isFixed = plan?.status === 'fixed'
  const isArchived = plan?.status === 'archived'
  const hasDirty = Object.keys(dirty).length > 0
  const hasRuns = runs.length > 0 || lastRunId !== null
  const activeRunId = selectedRunId ?? lastRunId ?? runs[0]?.run_id ?? null

  const loadMatrix = useCallback(async () => {
    setMatrixLoading(true)
    setMatrixError('')
    setDirty({})
    try {
      const data = await getPeriodPlanMatrix(planId)
      setMatrix(data)
      setPlan(data.plan)
    } catch (e) {
      setMatrixError(e instanceof Error ? e.message : String(e))
    } finally {
      setMatrixLoading(false)
    }
  }, [planId])

  const loadRuns = useCallback(async () => {
    try {
      const data = await listPeriodPlanRuns(planId)
      setRuns(data.rows ?? [])
      if (!selectedRunId && data.rows?.length) {
        setSelectedRunId(data.rows[0].run_id)
        setLastRunId((prev) => prev ?? data.rows[0].run_id)
      }
    } catch {
      // silent: empty plan with no runs is fine
    }
  }, [planId, selectedRunId])

  const loadJournal = useCallback(async (flow = journalFlow, runId?: number) => {
    setJournalLoading(true)
    setJournalError('')
    try {
      const data = await getExecutionJournal(planId, { flow: flow || undefined, run_id: runId })
      setJournal(data)
      setLastRunId(data.run_id)
      if (!selectedRunId) setSelectedRunId(data.run_id)
      setPlan(data.plan)
    } catch (e) {
      setJournalError(e instanceof Error ? e.message : String(e))
      setJournal(null)
    } finally {
      setJournalLoading(false)
    }
  }, [journalFlow, planId, selectedRunId])

  useEffect(() => { void loadMatrix() }, [loadMatrix])
  useEffect(() => { void loadRuns() }, [loadRuns])

  useEffect(() => {
    if (tab === 'matrix' && !matrix) void loadMatrix()
    if (tab === 'journal' && !journal) void loadJournal(journalFlow, activeRunId ?? undefined)
  }, [journal, loadJournal, loadMatrix, matrix, tab, journalFlow, activeRunId])

  // Autofocus search input on entering draft matrix
  useEffect(() => {
    if (tab === 'matrix' && isDraft && searchInputRef.current) {
      searchInputRef.current.focus()
    }
  }, [tab, isDraft])

  // Hotkeys: Esc back, F5 refresh
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null
      const inInput = !!target && /^(INPUT|TEXTAREA|SELECT)$/i.test(target.tagName)
      if (e.key === 'Escape' && !inInput) {
        e.preventDefault()
        onBack()
        return
      }
      if (e.key === 'F5') {
        e.preventDefault()
        if (tab === 'matrix') void loadMatrix()
        else void loadJournal(journalFlow, activeRunId ?? undefined)
        void loadRuns()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onBack, tab, loadMatrix, loadJournal, loadRuns, journalFlow, activeRunId])

  function nonEmptyMatrix() {
    if (!matrix) return false
    return matrix.rows.some((r) => matrix.buckets.some((b) => {
      const d = dirty[r.item_id]?.[b]
      const v = d !== undefined ? d : (r.buckets[b] ?? 0)
      return v > 0
    }))
  }

  async function handleFix() {
    if (hasDirty) {
      setError('Есть несохранённые изменения. Сначала «Сохранить» или «Отмена».')
      return
    }
    if (!nonEmptyMatrix()) {
      setError('Нельзя зафиксировать пустой план. Введите хотя бы одно ненулевое количество.')
      return
    }
    if (!confirm('Зафиксировать план? После фиксации редактирование уже введённых значений недоступно (можно только дозаполнять новые ячейки), и план можно прогнать через MRP-снимок.')) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      await fixPeriodPlan(planId)
      setMessage('План зафиксирован. Теперь доступна кнопка «MRP снимок».')
      await loadMatrix()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleArchive() {
    if (!confirm('Отправить план в архив? Архивные планы скрываются из активной работы.')) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      await archivePeriodPlan(planId)
      setMessage('План отправлен в архив')
      await loadMatrix()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleUnarchive() {
    setActing(true)
    setError('')
    setMessage('')
    try {
      await unarchivePeriodPlan(planId)
      setMessage('План возвращён из архива')
      await loadMatrix()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  function startEditHeader() {
    if (!plan) return
    setEditName(plan.name)
    setEditFrom(plan.period_from)
    setEditTo(plan.period_to)
    setEditComment(plan.comment ?? '')
    setEditingHeader(true)
  }

  async function handleSaveHeader() {
    if (!plan) return
    if (editFrom && editTo && editFrom > editTo) {
      setError('Дата окончания периода не может быть раньше даты начала')
      return
    }
    setActing(true)
    setError('')
    setMessage('')
    try {
      await updatePeriodPlanHeader(planId, {
        name: editName.trim() || undefined,
        period_from: editFrom || undefined,
        period_to: editTo || undefined,
        comment: editComment.trim() ? editComment.trim() : null,
      })
      setMessage('Шапка плана сохранена')
      setEditingHeader(false)
      await loadMatrix()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleSnapshot() {
    setActing(true)
    setError('')
    setMessage('')
    try {
      const result = await createMrpSnapshot(planId)
      setLastRunId(result.run_id)
      setSelectedRunId(result.run_id)
      setMessage(`MRP-снимок создан: run #${result.run_id}, требований: ${result.requirement_count}, закупок: ${result.purchase_count}, переработок: ${result.rework_count}`)
      setTab('journal')
      await Promise.all([loadJournal(journalFlow, result.run_id), loadRuns()])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleAllocate() {
    const runId = activeRunId
    if (!runId) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      const [p, r] = await Promise.all([allocatePurchases(runId), allocateRework(runId)])
      setMessage(`Аллокация: закупки ${p.updated_count} строк, переработки ${r.updated_count} строк`)
      await loadJournal(journalFlow, runId)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleCreateProductionOrders() {
    if (!journal) return
    const reqIds = journal.rows
      .filter((r) => r.flow === 'production' && r.remaining_qty > 1e-9)
      .map((r) => r.req_id)
    if (!reqIds.length) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      const result = await createProductionOrdersFromRequirements(reqIds)
      const created = (result.created as unknown[]).length
      const reused = (result.reused as unknown[]).length
      const skipped = (result.skipped as unknown[]).length
      const parts: string[] = []
      if (created) parts.push(`создано ${created}`)
      if (reused) parts.push(`уже было ${reused}`)
      if (skipped) parts.push(`пропущено ${skipped}`)
      setMessage(`Заказы производства: ${parts.join(', ') || 'нет изменений'}`)
      if (result.errors?.length) setError(result.errors.join('; '))
      await loadJournal(journalFlow, activeRunId ?? undefined)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleSaveMatrix() {
    if (!matrix) return
    const entries: Array<{ item_id: number; bucket_date: string; qty: number }> = []
    for (const [itemId, bucketMap] of Object.entries(dirty)) {
      for (const [bucket_date, q] of Object.entries(bucketMap)) {
        entries.push({ item_id: Number(itemId), bucket_date, qty: q })
      }
    }
    if (!entries.length) return
    setSaving(true)
    setMatrixError('')
    setMessage('')
    try {
      const result = await bulkUpsertPeriodPlanLines(planId, entries)
      const saved = (result as { saved?: number })?.saved ?? entries.length
      setDirty({})
      setMessage(`Сохранено строк: ${saved}`)
      await loadMatrix()
    } catch (e) {
      setMatrixError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function handleCellChange(itemId: number, bucket: string, value: string) {
    const num = Math.max(0, parseFloat(value) || 0)
    setDirty((prev) => ({
      ...prev,
      [itemId]: { ...(prev[itemId] ?? {}), [bucket]: num },
    }))
  }

  // Tab/Enter navigation between matrix inputs
  function cellRefKey(itemId: number, bucket: string) {
    return `${itemId}|${bucket}`
  }
  function handleCellKeyDown(e: React.KeyboardEvent<HTMLInputElement>, itemId: number, bucket: string) {
    if (!matrix) return
    const buckets = matrix.buckets
    const rows = matrix.rows
    const colIdx = buckets.indexOf(bucket)
    const rowIdx = rows.findIndex((r) => r.item_id === itemId)
    if (colIdx < 0 || rowIdx < 0) return
    let nextRow = rowIdx
    let nextCol = colIdx
    if (e.key === 'Enter' || (e.key === 'ArrowDown')) {
      nextRow = Math.min(rowIdx + 1, rows.length - 1)
    } else if (e.key === 'ArrowUp') {
      nextRow = Math.max(rowIdx - 1, 0)
    } else if (e.key === 'Tab' && !e.shiftKey) {
      if (colIdx < buckets.length - 1) nextCol = colIdx + 1
      else { nextCol = 0; nextRow = Math.min(rowIdx + 1, rows.length - 1) }
    } else if (e.key === 'Tab' && e.shiftKey) {
      if (colIdx > 0) nextCol = colIdx - 1
      else { nextCol = buckets.length - 1; nextRow = Math.max(rowIdx - 1, 0) }
    } else if (e.key === 'ArrowRight' && (e.currentTarget.selectionStart ?? 0) >= e.currentTarget.value.length) {
      nextCol = Math.min(colIdx + 1, buckets.length - 1)
    } else if (e.key === 'ArrowLeft' && (e.currentTarget.selectionStart ?? 0) === 0) {
      nextCol = Math.max(colIdx - 1, 0)
    } else {
      return
    }
    if (nextRow === rowIdx && nextCol === colIdx) return
    e.preventDefault()
    const next = matrixInputRefs.current[cellRefKey(rows[nextRow].item_id, buckets[nextCol])]
    next?.focus()
    next?.select()
  }

  function handleSpreadEvenly(itemId: number) {
    if (!matrix) return
    const row = matrix.rows.find((r) => r.item_id === itemId)
    if (!row) return
    const total = matrix.buckets.reduce((s, b) => s + cellValue(row, b), 0)
    if (!total) return
    const editable = matrix.buckets.filter((b) => !row.locked_buckets[b])
    if (!editable.length) return
    const per = Math.floor(total / editable.length)
    const rem = total - per * editable.length
    const newDirty: Record<string, number> = {}
    editable.forEach((b, i) => { newDirty[b] = per + (i < rem ? 1 : 0) })
    setDirty((prev) => ({ ...prev, [itemId]: { ...(prev[itemId] ?? {}), ...newDirty } }))
  }

  function handleCopyAcrossRow(itemId: number, sourceBucket: string) {
    if (!matrix) return
    const row = matrix.rows.find((r) => r.item_id === itemId)
    if (!row) return
    const val = cellValue(row, sourceBucket)
    const newDirty: Record<string, number> = {}
    matrix.buckets.forEach((b) => {
      if (!row.locked_buckets[b]) newDirty[b] = val
    })
    setDirty((prev) => ({ ...prev, [itemId]: { ...(prev[itemId] ?? {}), ...newDirty } }))
  }

  useEffect(() => {
    const query = searchQuery.trim()
    if (query.length < 2) {
      setSearchRows([])
      setSuggestOpen(false)
      setSearchHighlight(0)
      return
    }
    let cancelled = false
    setSearching(true)
    const handle = setTimeout(async () => {
      try {
        const result = await searchNomenclature(query)
        if (cancelled) return
        setSearchRows(result.items ?? [])
        setSuggestOpen(true)
        setSearchHighlight(0)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setSearching(false)
      }
    }, 200)
    return () => { cancelled = true; clearTimeout(handle) }
  }, [searchQuery])

  async function handleAddItem(item: NomenclatureSearchItem) {
    setActing(true)
    setError('')
    setMessage('')
    try {
      const ensured = await ensurePlanItem(item)
      await addItemToPeriodPlan(planId, ensured.item_id)
      setMessage(`Добавлено: ${item.item_name}`)
      setSearchQuery('')
      setSearchRows([])
      setSuggestOpen(false)
      await loadMatrix()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleDeleteRow(itemId: number, itemName: string) {
    if (!confirm(`Удалить строку «${itemName}» из плана?`)) return
    setDeletingItemId(itemId)
    setError('')
    setMessage('')
    try {
      await deleteItemFromPeriodPlan(planId, itemId)
      setMessage(`Строка удалена: ${itemName}`)
      await loadMatrix()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeletingItemId(null)
    }
  }

  function cellValue(row: { item_id: number; buckets: Record<string, number> }, bucket: string) {
    const d = dirty[row.item_id]?.[bucket]
    return d !== undefined ? d : (row.buckets[bucket] ?? 0)
  }

  function toggleJournalSort(key: JournalSortKey) {
    if (journalSortBy === key) setJournalSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setJournalSortBy(key); setJournalSortDir('asc') }
  }
  function jSortArrow(key: JournalSortKey) {
    if (journalSortBy !== key) return ''
    return journalSortDir === 'asc' ? ' ▲' : ' ▼'
  }

  const filteredJournalRows = useMemo(() => {
    if (!journal) return []
    let rows = journal.rows.slice()
    if (journalBomLevel !== '') {
      const lvl = Number(journalBomLevel)
      rows = rows.filter((r) => r.bom_level === lvl)
    }
    if (journalCoverage) {
      rows = rows.filter((r) => {
        if (journalCoverage === 'covered') return r.remaining_qty <= 0 && r.net_qty > 0
        if (journalCoverage === 'partial') return r.completed_qty > 0 && r.remaining_qty > 0
        if (journalCoverage === 'ordered') return r.ordered_qty > 0 && r.completed_qty <= 0
        if (journalCoverage === 'none') return r.ordered_qty <= 0 && r.completed_qty <= 0
        return true
      })
    }
    const dir = journalSortDir === 'asc' ? 1 : -1
    rows.sort((a, b) => {
      const va: unknown = (a as unknown as Record<JournalSortKey, unknown>)[journalSortBy]
      const vb: unknown = (b as unknown as Record<JournalSortKey, unknown>)[journalSortBy]
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir
      return String(va ?? '').localeCompare(String(vb ?? ''), 'ru') * dir
    })
    return rows
  }, [journal, journalBomLevel, journalCoverage, journalSortBy, journalSortDir])

  const bomLevels = useMemo(() => {
    if (!journal) return [] as number[]
    return Array.from(new Set(journal.rows.map((r) => r.bom_level))).sort((a, b) => a - b)
  }, [journal])

  function downloadJournalCsv() {
    if (!journal) return
    const rows = filteredJournalRows
    const headers = ['Артикул', 'Номенклатура', 'Поток', 'Уровень', 'Потребность', 'К запуску/заказу', 'В заказах', 'Выполнено', 'Осталось', 'Прогресс %', 'Заданий']
    const esc = (v: unknown) => {
      const s = String(v ?? '')
      return /[",;\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
    }
    const body = rows.map((r) => [r.item_article || r.item_code, r.item_name, flowLabel(r.flow), r.bom_level, r.gross_qty, r.net_qty, r.ordered_qty, r.completed_qty, r.remaining_qty, r.coverage_pct, r.work_items.length].map(esc).join(';'))
    const csv = '﻿' + [headers.join(';'), ...body].join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `period-plan-${planId}-journal${activeRunId ? '-run' + activeRunId : ''}.csv`
    document.body.appendChild(a); a.click(); a.remove()
    URL.revokeObjectURL(url)
  }

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  function workItemHref(_wi: { type: string; order_id?: number; purchase_id?: number; rework_id?: number; run_id?: number }) {
    // Cross-page deep linking is not wired; for now return null and just label the row.
    return null as string | null
  }

  // Column sums for matrix footer
  const matrixColSums = useMemo(() => {
    if (!matrix) return {} as Record<string, number>
    const sums: Record<string, number> = {}
    matrix.buckets.forEach((b) => { sums[b] = 0 })
    matrix.rows.forEach((row) => {
      matrix.buckets.forEach((b) => { sums[b] += cellValue(row, b) })
    })
    return sums
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matrix, dirty])

  const planTitle = plan ? plan.name : `План #${planId}`
  const planSubtitle = plan
    ? `${dateRu(plan.period_from)} — ${dateRu(plan.period_to)}${plan.comment ? ' · ' + plan.comment : ''}`
    : 'Загрузка…'

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование / Планирование выпуска / {planTitle}</div>
        {plan && <div className="runBadge">{periodPlanStatusLabel(plan.status)}</div>}
      </div>

      <DocumentWindow
        title={planTitle}
        subtitle={planSubtitle}
        hotkeys="Esc Назад · F5 Обновить"
        footer={(
          <StatusBar
            loading={matrixLoading || journalLoading || acting}
            visibleFrom={0}
            visibleTo={0}
            total={0}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={onBack}>К списку планов</button>
          <button onClick={() => { void loadMatrix(); void loadRuns() }} disabled={matrixLoading || acting}>Обновить</button>
          <div className="barSeparator" />
          {isDraft && (
            <>
              <button onClick={() => void handleFix()} disabled={acting || hasDirty}>Зафиксировать</button>
              {!editingHeader && <button onClick={startEditHeader} disabled={acting}>Изменить шапку</button>}
            </>
          )}
          {isFixed && (
            <button className="primary" onClick={() => void handleSnapshot()} disabled={acting}>MRP снимок</button>
          )}
          {hasRuns && (
            <button onClick={() => void handleAllocate()} disabled={acting || !activeRunId}>Ре-аллокация</button>
          )}
          {isFixed && (
            <button onClick={() => void handleArchive()} disabled={acting}>В архив</button>
          )}
          {isArchived && (
            <button onClick={() => void handleUnarchive()} disabled={acting}>Из архива</button>
          )}
          {hasDirty && tab === 'matrix' && (
            <>
              <div className="barSeparator" />
              <button className="primary" onClick={() => void handleSaveMatrix()} disabled={saving}>Сохранить</button>
              <button onClick={() => { setDirty({}); void loadMatrix() }} disabled={saving}>Отмена</button>
            </>
          )}
          {activeRunId && (
            <span className="toolbarText" style={{ marginLeft: 6 }}>Run #{activeRunId}</span>
          )}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        {plan && !editingHeader && (
          <div className="mrpSummaryStrip" style={{ gridTemplateColumns: 'repeat(6, minmax(100px,1fr))' }}>
            <div className="metricCell">
              <span>Название</span>
              <strong style={{ fontSize: 14, marginTop: 4 }}>{plan.name}</strong>
            </div>
            <div className="metricCell">
              <span>Статус</span>
              <strong><span className={`pill ${periodPlanStatusClass(plan.status)}`}>{periodPlanStatusLabel(plan.status)}</span></strong>
            </div>
            <div className="metricCell">
              <span>Период</span>
              <strong style={{ fontSize: 13 }}>{dateRu(plan.period_from)} — {dateRu(plan.period_to)}</strong>
            </div>
            <div className="metricCell">
              <span>Зафиксирован</span>
              <strong>{plan.fixed_at ? dateTimeRu(plan.fixed_at) : '—'}</strong>
              {plan.fixed_by && <em style={{ fontStyle: 'normal', color: 'var(--muted)', fontSize: 11 }}>{plan.fixed_by}</em>}
            </div>
            <div className="metricCell">
              <span>Создан</span>
              <strong style={{ fontSize: 12 }}>{plan.created_at ? dateTimeRu(plan.created_at) : '—'}</strong>
              {plan.created_by && <em style={{ fontStyle: 'normal', color: 'var(--muted)', fontSize: 11 }}>{plan.created_by}</em>}
            </div>
            <div className="metricCell">
              <span>Комментарий</span>
              <strong style={{ fontSize: 12 }}>{plan.comment ?? '—'}</strong>
            </div>
          </div>
        )}

        {plan && editingHeader && (
          <div className="requisites" style={{ gridTemplateColumns: 'minmax(220px,1fr) 160px 160px minmax(220px,1fr) auto auto' }}>
            <label>
              <span>Название плана</span>
              <input value={editName} onChange={(e) => setEditName(e.target.value)} />
            </label>
            <label>
              <span>Период с</span>
              <input type="date" value={editFrom} onChange={(e) => setEditFrom(e.target.value)} />
            </label>
            <label>
              <span>Период по</span>
              <input type="date" value={editTo} onChange={(e) => setEditTo(e.target.value)} />
            </label>
            <label>
              <span>Комментарий</span>
              <input value={editComment} onChange={(e) => setEditComment(e.target.value)} />
            </label>
            <button className="primary" style={{ alignSelf: 'end' }} onClick={() => void handleSaveHeader()} disabled={acting || !editName.trim()}>Сохранить шапку</button>
            <button style={{ alignSelf: 'end' }} onClick={() => setEditingHeader(false)}>Отмена</button>
          </div>
        )}

        <div className="tabsBar">
          <button className={tab === 'matrix' ? 'activeTab' : ''} onClick={() => setTab('matrix')}>Матрица</button>
          <button className={tab === 'journal' ? 'activeTab' : ''} onClick={() => setTab('journal')}>Журнал исполнения</button>
        </div>

        {tab === 'matrix' && isDraft && (
          <div className="planSearchBar" style={{ position: 'relative' }}>
            <label style={{ position: 'relative' }}>
              <span>
                Добавить номенклатуру в план
                {searching && <em style={{ marginLeft: 8, fontStyle: 'normal', color: 'var(--muted)' }}>поиск…</em>}
                {!searching && suggestOpen && searchRows.length > 0 && (
                  <em style={{ marginLeft: 8, fontStyle: 'normal', color: 'var(--muted)' }}>
                    найдено: {searchRows.length}
                  </em>
                )}
              </span>
              <input
                ref={searchInputRef}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onFocus={() => { if (searchRows.length) setSuggestOpen(true) }}
                onBlur={() => setTimeout(() => setSuggestOpen(false), 150)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    setSuggestOpen(true)
                    setSearchHighlight((h) => Math.min(h + 1, Math.max(0, searchRows.length - 1)))
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    setSearchHighlight((h) => Math.max(0, h - 1))
                  } else if (e.key === 'Enter' && searchRows.length) {
                    e.preventDefault()
                    const pick = searchRows[searchHighlight] ?? searchRows[0]
                    void handleAddItem(pick)
                  } else if (e.key === 'Escape') {
                    setSuggestOpen(false)
                  }
                }}
                placeholder="наименование / артикул / код (поиск по мере ввода, ↑/↓ выбор, Enter добавить)"
                autoComplete="off"
              />
              {suggestOpen && (
                <div
                  className="planSearchResults"
                  style={{
                    position: 'absolute',
                    top: '100%',
                    left: 0,
                    right: 0,
                    zIndex: 50,
                    maxHeight: 320,
                    overflowY: 'auto',
                    background: '#fff',
                    border: '1px solid #9fa7b2',
                    boxShadow: '0 8px 20px rgba(15,21,28,.18)',
                    marginTop: 2,
                  }}
                >
                  {searchRows.length === 0 && !searching && (
                    <div style={{ padding: '8px 10px', color: 'var(--muted)' }}>Ничего не найдено</div>
                  )}
                  {searchRows.map((item, idx) => (
                    <button
                      key={item.item_code}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => void handleAddItem(item)}
                      onMouseEnter={() => setSearchHighlight(idx)}
                      disabled={acting}
                      style={{
                        width: '100%',
                        textAlign: 'left',
                        background: idx === searchHighlight ? 'linear-gradient(#fff9e9,#fff2c7)' : undefined,
                      }}
                    >
                      <strong>{item.item_name || '—'}</strong>
                      <span>
                        Арт. {item.item_article || '—'} · {item.item_code}
                        {typeof item.similarity === 'number' && item.similarity < 1 && (
                          <em style={{ marginLeft: 6, fontStyle: 'normal', color: 'var(--muted)' }}>
                            схожесть {Math.round(item.similarity * 100)}%
                          </em>
                        )}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </label>
          </div>
        )}

        {/* Matrix tab */}
        {tab === 'matrix' && (
          <div className="tablePane resultTablePane" style={{ flex: 1 }}>
            {matrixLoading && <div className="hintLine">Загрузка матрицы…</div>}
            {matrixError && <div className="errorLine">{matrixError}</div>}
            {matrix && (
              <table className="journalTable" style={{ minWidth: `${528 + matrix.buckets.length * 90}px`, tableLayout: 'fixed' }}>
                <thead>
                  <tr>
                    <th style={{ width: 74 }}>Код</th>
                    <th style={{ width: 320 }}>Номенклатура</th>
                    <th style={{ width: 96, textAlign: 'right' }}>Итого</th>
                    {matrix.buckets.map((b) => (
                      <th key={b} style={{ width: 90, textAlign: 'right' }}>{bucketLabel(b)}</th>
                    ))}
                    {isDraft && <th style={{ width: 38, textAlign: 'center' }} title="Распределить итого равномерно по доступным неделям">≡</th>}
                    {isDraft && <th style={{ width: 34, textAlign: 'center' }}>×</th>}
                  </tr>
                </thead>
                <tbody>
                  {matrix.rows.map((row) => {
                    const rowLocked = Object.keys(row.locked_buckets).length > 0
                    return (
                      <tr key={row.item_id}>
                        <td><span className="muted">{row.item_code}</span></td>
                        <td>
                          <strong>{row.item_name}</strong>
                          {row.item_article && <span className="muted">{row.item_article}</span>}
                        </td>
                        <td className="numCell">
                          <strong>{qty(matrix.buckets.reduce((s, b) => s + cellValue(row, b), 0))}</strong>
                        </td>
                        {matrix.buckets.map((b) => {
                          const locked = row.locked_buckets[b] !== undefined
                          const val = cellValue(row, b)
                          const dirtyValue = dirty[row.item_id]?.[b]
                          const isDirtyCell = dirtyValue !== undefined && dirtyValue !== (row.buckets[b] ?? 0)
                          const forecast = row.bucket_forecasts?.[b]
                          if (isDraft && !locked) {
                            return (
                              <td
                                key={b}
                                className="weekPlanCell"
                                style={isDirtyCell ? { background: '#fff9e0' } : undefined}
                                onDoubleClick={() => handleCopyAcrossRow(row.item_id, b)}
                                title="Двойной клик — скопировать значение во все ячейки строки"
                              >
                                <input
                                  ref={(el) => { matrixInputRefs.current[cellRefKey(row.item_id, b)] = el }}
                                  type="number"
                                  min={0}
                                  step={1}
                                  value={val || ''}
                                  placeholder="0"
                                  onChange={(e) => handleCellChange(row.item_id, b, e.target.value)}
                                  onKeyDown={(e) => handleCellKeyDown(e, row.item_id, b)}
                                />
                                <ForecastShift forecast={forecast} />
                              </td>
                            )
                          }
                          return (
                            <td
                              key={b}
                              className="weekPlanCell"
                              style={locked ? { background: 'repeating-linear-gradient(135deg,#eef1f5 0 4px,#e3e8ee 4px 8px)' } : undefined}
                              title={locked ? 'Зафиксировано MRP-прогоном' : undefined}
                            >
                              <span style={{ display: 'block', textAlign: 'right', paddingRight: 4, color: locked ? 'var(--muted)' : undefined }}>
                                {val ? qty(val) : (isDraft ? '' : <span className="muted">—</span>)}
                              </span>
                              <ForecastShift forecast={forecast} />
                            </td>
                          )
                        })}
                        {isDraft && (
                          <td style={{ textAlign: 'center', padding: 0 }}>
                            <button
                              onClick={() => handleSpreadEvenly(row.item_id)}
                              disabled={rowLocked}
                              title={rowLocked ? 'Строка частично зафиксирована' : 'Распределить итого равномерно'}
                              style={{ minHeight: 22, padding: '0 6px' }}
                            >
                              ≡
                            </button>
                          </td>
                        )}
                        {isDraft && (
                          <td style={{ textAlign: 'center', padding: 0 }}>
                            <button
                              onClick={() => void handleDeleteRow(row.item_id, row.item_name)}
                              disabled={rowLocked || deletingItemId === row.item_id}
                              title={rowLocked ? 'Строка зафиксирована MRP-прогоном' : 'Удалить строку'}
                              style={{
                                minHeight: 22,
                                padding: '0 6px',
                                color: rowLocked ? 'var(--muted)' : 'var(--red)',
                                fontWeight: 700,
                              }}
                            >
                              ×
                            </button>
                          </td>
                        )}
                      </tr>
                    )
                  })}
                  {!matrix.rows.length && (
                    <tr><td colSpan={3 + matrix.buckets.length + (isDraft ? 2 : 0)}><div className="emptyDetail">Матрица пуста — добавьте номенклатуру через поиск выше</div></td></tr>
                  )}
                </tbody>
                {matrix.rows.length > 0 && (
                  <tfoot>
                    <tr style={{ position: 'sticky', bottom: 0, background: 'linear-gradient(#eef1f5,#dfe4ea)', fontWeight: 700 }}>
                      <td colSpan={2} style={{ textAlign: 'right', paddingRight: 6 }}>Итого по неделям</td>
                      <td className="numCell">
                        <strong>{qty(matrix.buckets.reduce((s, b) => s + (matrixColSums[b] ?? 0), 0))}</strong>
                      </td>
                      {matrix.buckets.map((b) => (
                        <td key={b} className="numCell"><strong>{qty(matrixColSums[b] ?? 0)}</strong></td>
                      ))}
                      {isDraft && <td />}
                      {isDraft && <td />}
                    </tr>
                  </tfoot>
                )}
              </table>
            )}
          </div>
        )}

        {/* Journal tab */}
        {tab === 'journal' && (
          <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
            <div className="commandBar">
              <button onClick={() => void loadJournal(journalFlow, activeRunId ?? undefined)} disabled={journalLoading}>Обновить</button>
              <button onClick={downloadJournalCsv} disabled={!journal || journalLoading}>CSV</button>
              <div className="barSeparator" />
              <button
                onClick={() => void handleCreateProductionOrders()}
                disabled={acting || !journal || !journal.rows.some((r) => r.flow === 'production' && r.remaining_qty > 1e-9)}
                title="Создать заказы производства для незакрытых строк производственного потока"
              >
                Создать заказы производства
              </button>
              <div className="barSeparator" />
              <label className="inlineControl">
                <span>Прогон</span>
                <select
                  value={String(selectedRunId ?? '')}
                  onChange={(e) => {
                    const v = e.target.value
                    const runId = v ? Number(v) : null
                    setSelectedRunId(runId)
                    void loadJournal(journalFlow, runId ?? undefined)
                  }}
                  disabled={!runs.length}
                >
                  {!runs.length && <option value="">—</option>}
                  {runs.map((r) => (
                    <option key={r.run_id} value={r.run_id}>
                      #{r.run_id} · {planningStatusLabel(r.status)} · {r.started_at ? dateTimeRu(r.started_at) : '—'}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inlineControl">
                <span>Поток</span>
                <select value={journalFlow} onChange={(e) => { setJournalFlow(e.target.value); void loadJournal(e.target.value, activeRunId ?? undefined) }}>
                  <option value="">Все</option>
                  <option value="production">Производство</option>
                  <option value="purchase">Закупка</option>
                  <option value="rework">Переработка</option>
                </select>
              </label>
              <label className="inlineControl">
                <span>BOM ур.</span>
                <select value={journalBomLevel} onChange={(e) => setJournalBomLevel(e.target.value)}>
                  <option value="">Все</option>
                  {bomLevels.map((lvl) => <option key={lvl} value={lvl}>{lvl}</option>)}
                </select>
              </label>
              <label className="inlineControl">
                <span>Статус</span>
                <select value={journalCoverage} onChange={(e) => setJournalCoverage(e.target.value)}>
                  <option value="">Все</option>
                  <option value="covered">Закрыто</option>
                  <option value="partial">Частично</option>
                  <option value="ordered">В заказах</option>
                  <option value="none">Без заданий</option>
                </select>
              </label>
              {journal && (
                <>
                  <div className="barSeparator" />
                  <span className="toolbarText">Закрыто: {journal.summary.fully_covered} / {journal.summary.total_items}</span>
                  {journal.summary.not_covered > 0 && (
                    <span style={{ color: 'var(--red)' }}>Не начато: {journal.summary.not_covered}</span>
                  )}
                  {journal.summary.partially_covered > 0 && (
                    <span style={{ color: 'var(--orange)' }}>Частично: {journal.summary.partially_covered}</span>
                  )}
                </>
              )}
            </div>

            {journalLoading && <div className="hintLine">Загрузка журнала…</div>}
            {journalError && <div className="errorLine">{journalError}</div>}
            {!journal && !journalLoading && !journalError && (
              <div className="emptyDetail" style={{ margin: 16 }}>
                MRP-снимок не создан. Зафиксируйте план и нажмите «MRP снимок».
              </div>
            )}

            {journal && (
              <div className="tablePane resultTablePane" style={{ flex: 1 }}>
                <table className="journalTable" style={{ minWidth: 1110 }}>
                  <thead>
                    <tr>
                      <th style={{ width: 88, cursor: 'pointer' }} onClick={() => toggleJournalSort('item_article')}>Артикул{jSortArrow('item_article')}</th>
                      <th style={{ width: 300, cursor: 'pointer' }} onClick={() => toggleJournalSort('item_name')}>Номенклатура{jSortArrow('item_name')}</th>
                      <th style={{ width: 104, cursor: 'pointer' }} onClick={() => toggleJournalSort('flow')}>Тип{jSortArrow('flow')}</th>
                      <th style={{ width: 52, textAlign: 'center', cursor: 'pointer' }} onClick={() => toggleJournalSort('bom_level')}>Ур.{jSortArrow('bom_level')}</th>
                      <th className="numCell" style={{ cursor: 'pointer' }} onClick={() => toggleJournalSort('gross_qty')}>Потребность{jSortArrow('gross_qty')}</th>
                      <th className="numCell" style={{ cursor: 'pointer' }} onClick={() => toggleJournalSort('net_qty')}>К запуску{jSortArrow('net_qty')}</th>
                      <th className="numCell" style={{ cursor: 'pointer' }} onClick={() => toggleJournalSort('ordered_qty')}>В заказах{jSortArrow('ordered_qty')}</th>
                      <th className="numCell" style={{ cursor: 'pointer' }} onClick={() => toggleJournalSort('completed_qty')}>Выполнено{jSortArrow('completed_qty')}</th>
                      <th className="numCell" style={{ cursor: 'pointer' }} onClick={() => toggleJournalSort('remaining_qty')}>Осталось{jSortArrow('remaining_qty')}</th>
                      <th style={{ width: 82, textAlign: 'center', cursor: 'pointer' }} onClick={() => toggleJournalSort('coverage_pct')}>Прогресс{jSortArrow('coverage_pct')}</th>
                      <th style={{ width: 64, textAlign: 'center' }}>Заданий</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredJournalRows.map((row) => (
                      <React.Fragment key={row.req_id}>
                        <tr
                          className={expandedReq === row.req_id ? 'activeRow' : ''}
                          style={{ cursor: row.work_items.length ? 'pointer' : undefined }}
                          onClick={() => setExpandedReq(expandedReq === row.req_id ? null : row.req_id)}
                        >
                          <td><span className="muted">{row.item_article || row.item_code}</span></td>
                          <td><strong>{row.item_name}</strong></td>
                          <td><span className={`miniPill ${flowClass(row.flow)}`}>{flowLabel(row.flow)}</span></td>
                          <td style={{ textAlign: 'center' }}>{row.bom_level}</td>
                          <td className="numCell"><strong>{qty(row.gross_qty)}</strong></td>
                          <td className="numCell"><strong>{qty(row.net_qty)}</strong></td>
                          <td className="numCell">{row.ordered_qty > 0 ? qty(row.ordered_qty) : <span className="muted">—</span>}</td>
                          <td className="numCell">{row.completed_qty > 0 ? qty(row.completed_qty) : <span className="muted">—</span>}</td>
                          <td className="numCell" style={{ color: row.remaining_qty > 0 ? 'var(--red)' : undefined }}>
                            {row.remaining_qty > 0 ? qty(row.remaining_qty) : '—'}
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            <span className={`miniPill ${coverageClass(row.coverage_pct)}`}>{row.coverage_pct}%</span>
                            <ForecastShift forecast={row} />
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            {row.work_items.length ? (
                              <span style={{ fontWeight: 700 }}>{row.work_items.length}</span>
                            ) : <span className="muted">—</span>}
                          </td>
                        </tr>
                        {expandedReq === row.req_id && row.work_items.map((wi, i) => {
                          const href = workItemHref(wi as unknown as { type: string; order_id?: number; purchase_id?: number; rework_id?: number; run_id?: number })
                          const label = wi.type === 'production_order'
                            ? `Заказ ${wi.order_number || '#' + wi.order_id}`
                            : wi.type === 'planned_order'
                              ? `Задание ${wi.order_id ? '#' + wi.order_id : ''}`
                              : wi.type === 'planned_purchase'
                                ? `Закупка ${wi.purchase_id ? '#' + wi.purchase_id : ''}`
                                : `Переработка ${wi.rework_id ? '#' + wi.rework_id : ''}`
                          return (
                            <tr key={`${row.req_id}-${i}`} style={{ background: '#f8fbff' }}>
                              <td />
                              <td colSpan={2} style={{ paddingLeft: 24 }}>
                                {href ? (
                                  <a href={href} className="muted">{label}</a>
                                ) : (
                                  <span className="muted">{label}</span>
                                )}
                              </td>
                              <td />
                              <td />
                              <td className="numCell"><strong>{qty(wi.qty)}</strong></td>
                              <td className="numCell">{wi.completed_qty !== undefined && wi.completed_qty > 0 ? qty(wi.completed_qty) : '—'}</td>
                              <td className="numCell">{wi.remaining_qty !== undefined ? qty(wi.remaining_qty) : '—'}</td>
                              <td />
                              <td style={{ textAlign: 'center' }}>
                                {wi.need_date ? <span className="muted">{dateRu(wi.need_date)}</span> : '—'}
                                <ForecastShift forecast={wi} />
                              </td>
                              <td />
                            </tr>
                          )
                        })}
                      </React.Fragment>
                    ))}
                    {!filteredJournalRows.length && (
                      <tr><td colSpan={11}><div className="emptyDetail">Нет данных по выбранному фильтру</div></td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </DocumentWindow>
    </main>
  )
}
