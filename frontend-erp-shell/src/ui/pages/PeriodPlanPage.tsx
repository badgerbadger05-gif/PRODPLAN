import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import type {
  ExecutionJournalResponse,
  ExecutionWorkItem,
  PeriodPlan,
  PeriodPlanMatrix,
  PeriodPlanRun,
} from '../../domain/planning'
import {
  coverageClass,
  executionFlowSummary,
  flowClass,
  flowLabel,
  journalRowStatus,
  journalRowStatusClass,
  journalRowStatusLabel,
  periodPlanStatusClass,
  periodPlanStatusLabel,
} from '../../domain/planning'
import type { NomenclatureSearchItem } from '../../domain/nomenclature'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import { searchNomenclature } from '../../services/nomenclature'
import type { MrpSnapshotResult } from '../../services/periodPlan'
import {
  addItemToPeriodPlan,
  bulkUpsertPeriodPlanLines,
  createMrpSnapshot,
  createPeriodPlan,
  deleteItemFromPeriodPlan,
  deletePeriodPlan,
  fixPeriodPlan,
  getExecutionJournal,
  getPeriodPlanMatrix,
  listPeriodPlanRuns,
  listPeriodPlans,
  closePeriodPlanRun,
  updatePeriodPlanHeader,
} from '../../services/periodPlan'
import { DocumentWindow } from '../layout/DocumentWindow'
import { RootProductFilterDialog, rootProductLabel, type RootProductOption } from '../RootProductFilterDialog'
import { StatusBar } from '../layout/StatusBar'
import { tableColumnStyle, tableMinWidth, type TableColumnDoctype } from '../tableDoctype'

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

/** Describe the snapshot the backend published, using only its own counters.
 *
 * On the idempotent branch (`immutable`) the backend returns the already
 * published snapshot and omits the counters; nothing is invented for them. */
function mrpSnapshotSummary(mrp: MrpSnapshotResult) {
  const parts = [`run #${mrp.run_id}`, `поколение #${mrp.ledger_generation_id}`]
  if (typeof mrp.requirement_count === 'number') parts.push(`требований: ${mrp.requirement_count}`)
  if (typeof mrp.production_count === 'number') parts.push(`производство: ${mrp.production_count}`)
  if (typeof mrp.purchase_count === 'number') parts.push(`закупок: ${mrp.purchase_count}`)
  if (typeof mrp.rework_count === 'number') parts.push(`переработок: ${mrp.rework_count}`)
  if (mrp.immutable) parts.push('снимок уже был опубликован')
  return parts.join(', ')
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

const periodPlanListColumns = [
  { key: 'name', title: 'Название', width: 240, minWidth: 240, grow: false, sortable: true },
  { key: 'status', title: 'Статус', width: 110, minWidth: 110, grow: false, sortable: true },
  { key: 'period_from', title: 'Период', width: 180, minWidth: 180, grow: false, sortable: true },
  { key: 'fixed_at', title: 'Зафиксирован', width: 140, minWidth: 140, grow: false, sortable: true },
  { key: 'fixed_by', title: 'Кем', width: 110, minWidth: 110, grow: false, sortable: false },
  { key: 'created_at', title: 'Создан', width: 140, minWidth: 140, grow: false, sortable: true },
  { key: 'line_count', title: 'Строк', width: 64, minWidth: 64, grow: false, align: 'right', sortable: false },
  { key: 'comment', title: 'Комментарий', minWidth: 240, grow: true, sortable: false },
] as const satisfies TableColumnDoctype[]

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
  const canDelete = selected?.status === 'draft'
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
          <table className="journalTable columnFilterTable" style={{ minWidth: tableMinWidth(periodPlanListColumns) }}>
            <colgroup>
              {periodPlanListColumns.map((column) => (
                <col key={column.key} style={tableColumnStyle(column)} />
              ))}
            </colgroup>
            <tbody>
              <tr>
                <td></td>
                <td>
                  <label className="columnFilterControl">
                    <span>Статус</span>
                    <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
                      <option value="">Все</option>
                      <option value="draft">Черновик</option>
                      <option value="fixed">Зафиксирован</option>
                      <option value="closed">Закрытые</option>
                    </select>
                  </label>
                </td>
                <td colSpan={2}>
                  <div className="columnFilterRange">
                    <label className="columnFilterControl">
                      <span>Период с</span>
                      <input type="date" value={filterFrom} onChange={(e) => setFilterFrom(e.target.value)} />
                    </label>
                    <label className="columnFilterControl">
                      <span>Период по</span>
                      <input type="date" value={filterTo} onChange={(e) => setFilterTo(e.target.value)} />
                    </label>
                  </div>
                </td>
                <td colSpan={2}>
                  <label className="columnFilterControl">
                    <span>Автор</span>
                    <input value={filterCreatedBy} onChange={(e) => setFilterCreatedBy(e.target.value)} placeholder="любая часть имени" />
                  </label>
                </td>
                <td></td>
                <td>
                  <button
                    className="columnFilterButton"
                    onClick={() => { setFilterStatus(''); setFilterFrom(''); setFilterTo(''); setFilterCreatedBy('') }}
                  >
                    Сбросить
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <table className="journalTable" style={{ minWidth: tableMinWidth(periodPlanListColumns), tableLayout: 'fixed' }}>
            <colgroup>
              {periodPlanListColumns.map((column) => (
                <col key={column.key} style={tableColumnStyle(column)} />
              ))}
            </colgroup>
            <thead>
              <tr>
                {periodPlanListColumns.map((column) => (
                  <th key={column.key} style={tableColumnStyle(column)}>
                    {column.sortable ? (
                      <button type="button" className="tableSortButton" onClick={() => toggleSort(column.key as SortKey)}>
                        {column.title}{sortArrow(column.key as SortKey)}
                      </button>
                    ) : column.title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {plans.map((plan) => (
                <tr
                  key={plan.id}
                  className={plan.id === selectedId ? 'activeRow' : ''}
                  style={{ cursor: 'pointer', opacity: plan.status === 'closed' ? 0.62 : undefined }}
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
  | 'net_qty'
  | 'ordered_qty'
  | 'completed_qty'
  | 'remaining_qty'
  | 'need_date'
  | 'status'
  | 'coverage_pct'

const periodPlanJournalColumns = [
  { key: 'item_article', title: 'Артикул', width: 108, minWidth: 108, grow: false, sortable: true },
  { key: 'item_name', title: 'Номенклатура', minWidth: 300, grow: true, sortable: true },
  { key: 'flow', title: 'Тип', width: 136, minWidth: 136, grow: false, sortable: true, tooltip: 'Способ пополнения: производство, закупка или переработка' },
  { key: 'bom_level', title: 'Ур.', width: 68, minWidth: 68, grow: false, align: 'center', sortable: true, tooltip: 'Уровень в дереве спецификации (0 — изделие плана)' },
  { key: 'net_qty', title: 'Потребность', width: 116, minWidth: 116, grow: false, align: 'right', className: 'numCell', sortable: true, tooltip: 'Чистая потребность = потребность с припусками − остаток склада. Брутто и склад — в подсказке ячейки' },
  { key: 'ordered_qty', title: 'Оформлено', width: 116, minWidth: 116, grow: false, align: 'right', className: 'numCell', sortable: true, tooltip: 'Оформлено в заказы (1С). Красным — есть неоформленный остаток, см. подсказку ячейки' },
  { key: 'completed_qty', title: 'Выполнено', width: 116, minWidth: 116, grow: false, align: 'right', className: 'numCell', sortable: true, tooltip: 'Выпущено производством / принято на склад' },
  { key: 'remaining_qty', title: 'Осталось', width: 110, minWidth: 110, grow: false, align: 'right', className: 'numCell', sortable: true, tooltip: 'Осталось выполнить до закрытия потребности' },
  { key: 'need_date', title: 'Срок', width: 118, minWidth: 118, grow: false, align: 'center', sortable: true, tooltip: 'Дата потребности; рядом — прогнозный сдвиг (+N дн = опоздание)' },
  { key: 'status', title: 'Статус', width: 128, minWidth: 128, grow: false, align: 'center', sortable: true, tooltip: 'Закрыто — выполнено полностью; Частично — есть выполнение; Оформлено — заказы созданы, выполнения нет; Не оформлено — требуются заказы; Покрыто складом — потребность закрыта остатком' },
  { key: 'coverage_pct', title: 'Выполнение', width: 104, minWidth: 104, grow: false, align: 'center', sortable: true, tooltip: '% выполнения от чистой потребности' },
  { key: 'work_items', title: 'Заданий', width: 72, minWidth: 72, grow: false, align: 'center', sortable: false, tooltip: 'Число заказов/заданий по строке; клик по строке раскрывает список' },
] as const satisfies TableColumnDoctype[]

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
  const [journalShowNetZero, setJournalShowNetZero] = useState(false)
  const [journalRootItemId, setJournalRootItemId] = useState<number | null>(null)
  const [journalRootDialogOpen, setJournalRootDialogOpen] = useState(false)
  const [journalSortBy, setJournalSortBy] = useState<JournalSortKey>('bom_level')
  const [journalSortDir, setJournalSortDir] = useState<SortDir>('asc')
  const [expandedReq, setExpandedReq] = useState<number | null>(null)
  const [lastRunId, setLastRunId] = useState<number | null>(null)

  const [runs, setRuns] = useState<PeriodPlanRun[]>([])
  const [runsLoaded, setRunsLoaded] = useState(false)
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
  const hasDirty = Object.keys(dirty).length > 0
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
      setRunsLoaded(true)
      if (!selectedRunId && data.rows?.length) {
        setSelectedRunId(data.rows[0].run_id)
        setLastRunId((prev) => prev ?? data.rows[0].run_id)
      }
    } catch {
      // silent: empty plan with no runs is fine
    }
  }, [planId, selectedRunId])

  const loadJournal = useCallback(async (flow = journalFlow, runId?: number, rootItemId = journalRootItemId) => {
    setJournalLoading(true)
    setJournalError('')
    try {
      const data = await getExecutionJournal(planId, { flow: flow || undefined, run_id: runId, root_item_id: rootItemId })
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
  }, [journalFlow, journalRootItemId, planId, selectedRunId])

  useEffect(() => { void loadMatrix() }, [loadMatrix])
  useEffect(() => { void loadRuns() }, [loadRuns])

  useEffect(() => {
    if (tab === 'matrix' && !matrix) void loadMatrix()
    if (tab === 'journal' && !journal) void loadJournal(journalFlow, activeRunId ?? undefined, journalRootItemId)
  }, [journal, loadJournal, loadMatrix, matrix, tab, journalFlow, activeRunId, journalRootItemId])

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
        else void loadJournal(journalFlow, activeRunId ?? undefined, journalRootItemId)
        void loadRuns()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onBack, tab, loadMatrix, loadJournal, loadRuns, journalFlow, activeRunId, journalRootItemId])

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
    if (!confirm('Зафиксировать план? Фиксация атомарна: план становится неизменяемым и в той же транзакции публикуется MRP-снимок одного поколения Ledger. Отдельного шага «MRP» нет.')) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      const fixed = await fixPeriodPlan(planId)
      const mrp = fixed.mrp
      if (mrp) {
        setLastRunId(mrp.run_id)
        setSelectedRunId(mrp.run_id)
        setMessage(`План зафиксирован, MRP-снимок опубликован: ${mrpSnapshotSummary(mrp)}`)
        setTab('journal')
        await Promise.all([
          loadMatrix(),
          loadJournal(journalFlow, mrp.run_id, journalRootItemId),
          loadRuns(),
        ])
      } else {
        setMessage('План зафиксирован')
        await Promise.all([loadMatrix(), loadRuns()])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleClose() {
    if (!activeRunId || !confirm('Закрыть план? Остаток не переносится автоматически.')) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      await closePeriodPlanRun(activeRunId)
      setMessage('План закрыт, история выполнения сохранена')
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

  /** Emergency path only: a plan that is already `fixed` but has no run at all,
   * i.e. its snapshot was lost or never published (older fixations, restored
   * database). The canonical flow publishes the snapshot inside «Зафиксировать»,
   * so this button is hidden as soon as the plan has a run. */
  async function handleSnapshotRecovery() {
    setActing(true)
    setError('')
    setMessage('')
    try {
      const result = await createMrpSnapshot(planId)
      setLastRunId(result.run_id)
      setSelectedRunId(result.run_id)
      setMessage(`MRP-снимок восстановлен: ${mrpSnapshotSummary(result)}`)
      setTab('journal')
      await Promise.all([loadJournal(journalFlow, result.run_id, journalRootItemId), loadRuns()])
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

  // Строка плана добавляется только по существующей номенклатуре: её ведёт
  // синхронизация из 1С. Локального создания и переименования позиций из UI
  // больше нет — поиск обязателен, `item_id` берётся прямо из его результата.
  async function handleAddItem(item: NomenclatureSearchItem) {
    setActing(true)
    setError('')
    setMessage('')
    try {
      await addItemToPeriodPlan(planId, item.item_id)
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
    if (!journalShowNetZero && journalCoverage !== 'net_zero') {
      rows = rows.filter((r) => journalRowStatus(r) !== 'net_zero')
    }
    if (journalCoverage) {
      rows = rows.filter((r) => journalRowStatus(r) === journalCoverage)
    }
    const dir = journalSortDir === 'asc' ? 1 : -1
    rows.sort((a, b) => {
      if (journalSortBy === 'status') {
        return journalRowStatusLabel(journalRowStatus(a)).localeCompare(journalRowStatusLabel(journalRowStatus(b)), 'ru') * dir
      }
      const va: unknown = (a as unknown as Record<JournalSortKey, unknown>)[journalSortBy]
      const vb: unknown = (b as unknown as Record<JournalSortKey, unknown>)[journalSortBy]
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir
      return String(va ?? '').localeCompare(String(vb ?? ''), 'ru') * dir
    })
    return rows
  }, [journal, journalBomLevel, journalCoverage, journalShowNetZero, journalSortBy, journalSortDir])

  const bomLevels = useMemo(() => {
    if (!journal) return [] as number[]
    return Array.from(new Set(journal.rows.map((r) => r.bom_level))).sort((a, b) => a - b)
  }, [journal])

  // `execution_pct === null` is the backend's explicit "недоступно"
  // (period_plan_service._unavailable_execution_journal). The frontend never
  // recomputes it from the rows — a fabricated percentage would look like a
  // real figure.
  const journalExecutionPct = useMemo(() => {
    if (!journal) return null
    return typeof journal.summary.execution_pct === 'number' ? journal.summary.execution_pct : null
  }, [journal])

  const journalExecutionByFlow = useMemo(
    () => (journal ? executionFlowSummary(journal.summary.execution_by_flow) : []),
    [journal],
  )

  const rootOptions = useMemo<RootProductOption[]>(() => (
    (matrix?.rows ?? []).map((row) => ({
      item_id: row.item_id,
      item_name: row.item_name,
      item_article: row.item_article,
      item_code: row.item_code,
    }))
  ), [matrix])

  function downloadJournalCsv() {
    if (!journal) return
    const rows = filteredJournalRows
    const headers = ['Артикул', 'Номенклатура', 'Поток', 'Уровень', 'Потребность (брутто)', 'Чистая потребность', 'Оформлено', 'Не оформлено', 'Выполнено', 'Осталось выполнить', 'Срок', 'Статус', 'Выполнение %', 'Заданий']
    const esc = (v: unknown) => {
      const s = String(v ?? '')
      return /[",;\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
    }
    const body = rows.map((r) => [r.item_article || r.item_code, r.item_name, flowLabel(r.flow), r.bom_level, r.gross_qty, r.net_qty, r.ordered_qty, r.unassigned_qty ?? 0, r.completed_qty, r.remaining_qty, r.need_date ?? '', journalRowStatusLabel(journalRowStatus(r)), r.coverage_pct, r.work_items.length].map(esc).join(';'))
    const csv = '﻿' + [headers.join(';'), ...body].join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `period-plan-${planId}-journal${activeRunId ? '-run' + activeRunId : ''}.csv`
    document.body.appendChild(a); a.click(); a.remove()
    URL.revokeObjectURL(url)
  }

  function workItemHref(wi: { type: string; product_id?: number; order_id?: number; purchase_id?: number; rework_id?: number; run_id?: number; one_c_opened?: boolean; order_number?: string }) {
    if (wi.type === 'production_order') {
      if (wi.product_id) return `#/production-control?product_id=${encodeURIComponent(String(wi.product_id))}`
      if (wi.order_id) return `#/production-control?order_id=${encodeURIComponent(String(wi.order_id))}`
      return null as string | null
    }
    if (wi.type === 'planned_purchase' && wi.one_c_opened && wi.order_number) {
      return `#/purchase-control?search=${encodeURIComponent(wi.order_number)}`
    }
    const runId = wi.run_id ?? activeRunId ?? journal?.run_id
    if (!runId) return null as string | null
    const search = new URLSearchParams()
    if (wi.type === 'planned_order') {
      search.set('tab', 'production')
      if (wi.order_id) search.set('planned_order_id', String(wi.order_id))
    } else if (wi.type === 'planned_purchase') {
      search.set('tab', 'purchases')
      if (wi.purchase_id) search.set('purchase_id', String(wi.purchase_id))
    } else if (wi.type === 'planned_rework') {
      search.set('tab', 'rework')
      if (wi.rework_id) search.set('rework_id', String(wi.rework_id))
    } else {
      return null as string | null
    }
    return `#/mrp-runs/${encodeURIComponent(String(runId))}?${search.toString()}`
  }

  function workItemAssignedQty(wi: ExecutionWorkItem) {
    return wi.type === 'production_order' || wi.type === 'planned_rework' || (wi.type === 'planned_purchase' && wi.one_c_opened)
      ? wi.qty
      : null
  }

  function workItemUnassignedQty(wi: ExecutionWorkItem) {
    return wi.type === 'planned_order' || (wi.type === 'planned_purchase' && !wi.one_c_opened)
      ? wi.qty
      : null
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
          {isFixed && runsLoaded && !runs.length && (
            <button
              onClick={() => void handleSnapshotRecovery()}
              disabled={acting}
              title="Аварийное восстановление: план зафиксирован, но ни одного прогона/снимка у него нет"
            >
              Восстановить MRP-снимок
            </button>
          )}
          {isFixed && activeRunId && (
            <button onClick={() => void handleClose()} disabled={acting}>Закрыть план</button>
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
              <button onClick={() => void loadJournal(journalFlow, activeRunId ?? undefined, journalRootItemId)} disabled={journalLoading}>Обновить</button>
              <button onClick={downloadJournalCsv} disabled={!journal || journalLoading}>CSV</button>
              <div className="barSeparator" />
              {journal && (
                <>
                  <div className="barSeparator" />
                  <span className="toolbarText" title="Выполнено / чистая потребность по всем строкам">
                    Общее выполнение: {journalExecutionPct === null ? 'недоступно' : `${journalExecutionPct}%`}
                  </span>
                  {journalExecutionByFlow.map((row) => (
                    <span key={row.flow} className="toolbarText">{row.label}: {row.text}</span>
                  ))}
                  <button
                    className="filterBtn"
                    onClick={() => setJournalCoverage((v) => (v === 'covered' ? '' : 'covered'))}
                    title="Показать только закрытые строки"
                  >
                    Закрыто: {journal.summary.fully_covered} / {journal.summary.total_items}
                  </button>
                  {journal.summary.not_covered > 0 && (
                    <button
                      className="filterBtn"
                      style={{ color: 'var(--red)' }}
                      onClick={() => setJournalCoverage((v) => (v === 'none' ? '' : 'none'))}
                      title="Показать только строки без оформленных заказов"
                    >
                      Не начато: {journal.summary.not_covered}
                    </button>
                  )}
                  {journal.summary.partially_covered > 0 && (
                    <button
                      className="filterBtn"
                      style={{ color: 'var(--orange)' }}
                      onClick={() => setJournalCoverage((v) => (v === 'partial' ? '' : 'partial'))}
                      title="Показать только частично выполненные строки"
                    >
                      Частично: {journal.summary.partially_covered}
                    </button>
                  )}
                  {journal.summary.net_zero > 0 && (
                    <label className="toolbarText" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer' }} title="Строки, потребность которых полностью закрыта остатком склада">
                      <input
                        type="checkbox"
                        checked={journalShowNetZero}
                        onChange={(e) => setJournalShowNetZero(e.target.checked)}
                      />
                      Покрытые складом: {journal.summary.net_zero}
                    </label>
                  )}
                </>
              )}
            </div>

            {journalLoading && <div className="hintLine">Загрузка журнала…</div>}
            {journalError && <div className="errorLine">{journalError}</div>}
            {!journal && !journalLoading && !journalError && (
              <div className="emptyDetail" style={{ margin: 16 }}>
                {isDraft
                  ? 'Журнал появится после фиксации: она атомарно публикует MRP-снимок плана.'
                  : runsLoaded && !runs.length
                    ? 'У плана нет опубликованного MRP-снимка. Восстановите его кнопкой «Восстановить MRP-снимок».'
                    : 'Журнал выбранного прогона не загружен.'}
              </div>
            )}

            {journal && (
              <div className="tablePane resultTablePane" style={{ flex: 1 }}>
                <table className="journalTable columnFilterTable" style={{ minWidth: tableMinWidth(periodPlanJournalColumns), tableLayout: 'fixed' }}>
                  <colgroup>
                    {periodPlanJournalColumns.map((column) => (
                      <col key={column.key} style={tableColumnStyle(column)} />
                    ))}
                  </colgroup>
                  <tbody>
                    <tr>
                      <td colSpan={2}>
                        <label className="columnFilterControl">
                          <span>Корневое изделие</span>
                          <button className="filterBtn columnFilterButton" onClick={() => setJournalRootDialogOpen(true)}>
                            {rootProductLabel(rootOptions, journalRootItemId)}
                          </button>
                        </label>
                      </td>
                      <td>
                        <label className="columnFilterControl">
                          <span>Поток</span>
                          <select value={journalFlow} onChange={(e) => { setJournalFlow(e.target.value); void loadJournal(e.target.value, activeRunId ?? undefined, journalRootItemId) }}>
                            <option value="">Все</option>
                            <option value="production">Производство</option>
                            <option value="purchase">Закупка</option>
                            <option value="rework">Переработка</option>
                          </select>
                        </label>
                      </td>
                      <td>
                        <label className="columnFilterControl">
                          <span>BOM ур.</span>
                          <select value={journalBomLevel} onChange={(e) => setJournalBomLevel(e.target.value)}>
                            <option value="">Все</option>
                            {bomLevels.map((lvl) => <option key={lvl} value={lvl}>{lvl}</option>)}
                          </select>
                        </label>
                      </td>
                      <td colSpan={4} />
                      <td />
                      <td>
                        <label className="columnFilterControl">
                          <span>Статус</span>
                          <select value={journalCoverage} onChange={(e) => setJournalCoverage(e.target.value)}>
                            <option value="">Все</option>
                            <option value="covered">Закрыто</option>
                            <option value="partial">Частично</option>
                            <option value="ordered">Оформлено</option>
                            <option value="none">Не оформлено</option>
                            <option value="net_zero">Покрыто складом</option>
                          </select>
                        </label>
                      </td>
                      <td colSpan={2} />
                    </tr>
                  </tbody>
                </table>
                <table className="journalTable" style={{ minWidth: tableMinWidth(periodPlanJournalColumns), tableLayout: 'fixed' }}>
                  <colgroup>
                    {periodPlanJournalColumns.map((column) => (
                      <col key={column.key} style={tableColumnStyle(column)} />
                    ))}
                  </colgroup>
                  <thead>
                    <tr>
                      {periodPlanJournalColumns.map((column) => {
                        const columnDoctype = column as TableColumnDoctype
                        return (
                          <th key={column.key} className={columnDoctype.className} style={tableColumnStyle(column)} title={columnDoctype.tooltip}>
                            {column.sortable ? (() => {
                              const sortKey: JournalSortKey = column.key
                              return (
                                <button type="button" className="tableSortButton" onClick={() => toggleJournalSort(sortKey)}>
                                {column.title}{jSortArrow(sortKey)}
                              </button>
                              )
                            })() : column.title}
                          </th>
                        )
                      })}
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
                          <td
                            className="numCell"
                            title={`Потребность с припусками: ${qty(row.gross_qty)} · Остаток склада: ${qty(row.stock_qty ?? Math.max(0, row.gross_qty - row.net_qty))}`}
                          >
                            <strong>{qty(row.net_qty)}</strong>
                          </td>
                          <td
                            className="numCell"
                            style={{ color: (row.unassigned_qty ?? 0) > 0 ? 'var(--red)' : undefined }}
                            title={(row.unassigned_qty ?? 0) > 0 ? `Не оформлено в заказы: ${qty(row.unassigned_qty ?? 0)}` : undefined}
                          >
                            {row.ordered_qty > 0 || (row.unassigned_qty ?? 0) > 0 ? qty(row.ordered_qty) : <span className="muted">—</span>}
                          </td>
                          <td className="numCell">{row.completed_qty > 0 ? qty(row.completed_qty) : <span className="muted">—</span>}</td>
                          <td className="numCell" style={{ color: row.remaining_qty > 0 ? 'var(--red)' : undefined }}>
                            {row.remaining_qty > 0 ? qty(row.remaining_qty) : '—'}
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            {row.need_date ? dateRu(row.need_date) : '—'}
                            <ForecastShift forecast={row} />
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            <span className={`miniPill ${journalRowStatusClass(journalRowStatus(row))}`}>
                              {journalRowStatusLabel(journalRowStatus(row))}
                            </span>
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            <span className={`miniPill ${coverageClass(row.coverage_pct)}`}>{row.coverage_pct}%</span>
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            {row.work_items.length ? (
                              <span style={{ fontWeight: 700 }}>{row.work_items.length}</span>
                            ) : <span className="muted">—</span>}
                          </td>
                        </tr>
                        {expandedReq === row.req_id && row.work_items.map((wi, i) => {
                          const href = workItemHref(wi as unknown as { type: string; product_id?: number; order_id?: number; purchase_id?: number; rework_id?: number; run_id?: number })
                          const assignedQty = workItemAssignedQty(wi)
                          const unassignedQty = workItemUnassignedQty(wi)
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
                              <td colSpan={11} style={{ paddingLeft: 24 }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                                  {href ? (
                                    <a href={href} title="Открыть источник">{label}</a>
                                  ) : (
                                    <span>{label}</span>
                                  )}
                                  {wi.type === 'production_order' && (
                                    <span
                                      className={`miniPill ${wi.one_c_opened ? 'ready' : 'partial'}`}
                                      title={wi.one_c_opened && wi.order_ref1c ? `1C Ref_Key: ${wi.order_ref1c}` : 'Внутренний заказ PRODPLAN, ещё не открыт в 1С'}
                                    >
                                      {wi.one_c_opened ? 'Открыт в 1С' : 'Внутренний заказ'}
                                    </span>
                                  )}
                                  {wi.type === 'planned_purchase' && (
                                    wi.one_c_opened ? (
                                      <span
                                        className={`miniPill ${(wi.completed_qty ?? 0) > 0 ? 'ready' : 'partial'}`}
                                        title={wi.order_state || (wi.order_ref1c ? `1C Ref_Key: ${wi.order_ref1c}` : 'Заказ поставщику в 1С')}
                                      >
                                        {(wi.completed_qty ?? 0) > 0 ? 'Принят на склад' : `Заказ в 1С${wi.order_number ? ' ' + wi.order_number : ''}`}
                                      </span>
                                    ) : (
                                      <span className="miniPill to_move" title="Плановая закупка MRP, заказ поставщику ещё не создан">План MRP</span>
                                    )
                                  )}
                                  {(wi.type === 'planned_order' || wi.type === 'planned_rework') && (
                                    <span className="miniPill to_move" title="Плановое задание MRP, заказ ещё не создан">План MRP</span>
                                  )}
                                  <span className="muted">
                                    оформлено: <strong>{assignedQty !== null ? qty(assignedQty) : '—'}</strong>
                                    {unassignedQty !== null && unassignedQty > 0 && (
                                      <> · не оформлено: <strong style={{ color: 'var(--red)' }}>{qty(unassignedQty)}</strong></>
                                    )}
                                    {' '}· выполнено: <strong>{wi.completed_qty !== undefined && wi.completed_qty > 0 ? qty(wi.completed_qty) : '—'}</strong>
                                    {' '}· осталось: <strong>{wi.remaining_qty !== undefined ? qty(wi.remaining_qty) : '—'}</strong>
                                    {wi.need_date && <> · срок: <strong>{dateRu(wi.need_date)}</strong></>}
                                  </span>
                                  <ForecastShift forecast={wi} />
                                </div>
                              </td>
                            </tr>
                          )
                        })}
                      </React.Fragment>
                    ))}
                    {!filteredJournalRows.length && (
                      <tr><td colSpan={12}><div className="emptyDetail">Нет данных по выбранному фильтру</div></td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </DocumentWindow>
      <RootProductFilterDialog
        open={journalRootDialogOpen}
        options={rootOptions}
        value={journalRootItemId}
        onApply={(value) => {
          setJournalRootItemId(value)
          setJournalRootDialogOpen(false)
          void loadJournal(journalFlow, activeRunId ?? undefined, value)
        }}
        onClose={() => setJournalRootDialogOpen(false)}
      />
    </main>
  )
}
