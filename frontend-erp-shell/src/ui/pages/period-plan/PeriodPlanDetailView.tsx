import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ExecutionJournalLedgerLinkEvent,
  ExecutionJournalRow,
  ExecutionJournalResponse,
  ExecutionWorkItem,
  PeriodPlan,
  PeriodPlanMatrix,
  PeriodPlanRun,
} from '../../../domain/planning'
import {
  coverageClass,
  flowClass,
  flowLabel,
  journalRowStatus,
  journalRowStatusClass,
  journalRowStatusLabel,
  isPlanningTruthAccepted,
  periodPlanStatusClass,
  periodPlanStatusLabel,
} from '../../../domain/planning'
import type { NomenclatureSearchItem } from '../../../domain/productionPlan'
import { dateRu, dateTimeRu, qty } from '../../../lib/format'
import { ensurePlanItem, searchNomenclature } from '../../../services/productionPlan'
import {
  addItemToPeriodPlan,
  reconcileRun,
  archivePeriodPlan,
  bulkUpsertPeriodPlanLines,
  createMrpSnapshot,
  createProductionOrdersFromRequirements,
  deleteItemFromPeriodPlan,
  fixPeriodPlan,
  getExecutionJournal,
  getPeriodPlanMatrix,
  listPeriodPlanRuns,
  unarchivePeriodPlan,
  updatePeriodPlanHeader,
} from '../../../services/periodPlan'
import { DocumentWindow } from '../../layout/DocumentWindow'
import { RootProductFilterDialog } from '../../RootProductFilterDialog'
import { rootProductLabel, type RootProductOption } from '../../rootProductOptions'
import { StatusBar } from '../../layout/StatusBar'
import { KeyboardShortcutShell, type KeyboardShortcut } from '../../platform'
import { tableColumnStyle, tableMinWidth, type TableColumnDoctype } from '../../tableDoctype'
import { bucketLabel, type SortDir } from './helpers'
import { ForecastShift } from './ForecastShift'

type Tab = 'matrix' | 'journal'

type JournalCoverageFilter = '' | 'covered' | 'partial' | 'ordered' | 'none' | 'net_zero' | 'incomplete'

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

export function PeriodPlanDetailView({ planId, onBack }: DetailViewProps) {
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
  const [journalCoverage, setJournalCoverage] = useState<JournalCoverageFilter>('')
  const [journalShowNetZero, setJournalShowNetZero] = useState(false)
  const [journalRootItemId, setJournalRootItemId] = useState<number | null>(null)
  const [journalRootDialogOpen, setJournalRootDialogOpen] = useState(false)
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
  const searchBlurTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (searchBlurTimerRef.current) clearTimeout(searchBlurTimerRef.current)
  }, [])
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

  const shortcuts = useMemo<KeyboardShortcut[]>(() => [
    {
      id: 'period-plan-detail-back',
      keys: 'Escape',
      scope: 'resource',
      run: onBack,
    },
    {
      id: 'period-plan-detail-reload',
      keys: 'F5',
      scope: 'resource',
      allowInEditable: true,
      allowInInteractive: true,
      run: () => {
        if (tab === 'matrix') void loadMatrix()
        else void loadJournal(journalFlow, activeRunId ?? undefined, journalRootItemId)
        void loadRuns()
      },
    },
  ], [activeRunId, journalFlow, journalRootItemId, loadJournal, loadMatrix, loadRuns, onBack, tab])

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
      await Promise.all([loadJournal(journalFlow, result.run_id, journalRootItemId), loadRuns()])
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
      const res = await reconcileRun(runId)
      const prod = res.production_added?.length ?? 0
      const purch = res.purchase_added?.length ?? 0
      const moved = res.rescheduled?.floating ?? 0
      setMessage(`Пересчёт: производство +${prod}, закупки +${purch}, перепланировано ${moved} строк`)
      await loadJournal(journalFlow, runId, journalRootItemId)
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
      await loadJournal(journalFlow, activeRunId ?? undefined, journalRootItemId)
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
    if (!journalShowNetZero && journalCoverage !== 'net_zero') {
      rows = rows.filter((r) => journalRowStatus(r) !== 'net_zero')
    }
    if (journalCoverage) {
      if (journalCoverage === 'incomplete') {
        rows = rows.filter((r) => {
          const status = journalRowStatus(r)
          return status === 'partial' || status === 'ordered' || status === 'none'
        })
      } else {
        rows = rows.filter((r) => journalRowStatus(r) === journalCoverage)
      }
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

  const journalExecutionPct = useMemo(() => {
    if (!journal || !isPlanningTruthAccepted(journal)) return null
    if (typeof journal.summary.execution_pct === 'number') return journal.summary.execution_pct
    if (journal.summary.execution_pct === null) return null
    const base = journal.rows.reduce((sum, row) => sum + (row.progress_base_qty ?? row.net_qty ?? 0), 0)
    if (base <= 1e-9) return 100
    const completed = journal.rows.reduce((sum, row) => sum + (row.completed_qty ?? 0), 0)
    return Math.round((completed / base) * 1000) / 10
  }, [journal])

  const journalExecutionByFlow = useMemo(() => {
    const purchaseRowBase = {
      flow: 'purchase',
      label: flowLabel('purchase'),
      pct: null,
      confirmedPct: null,
      toOrderPct: null,
      base: 0,
      available: false,
    } as const
    if (!journal) return [] as Array<{
      flow: string
      label: string
      pct: number | null
      confirmedPct: number | null
      toOrderPct: number | null
      base: number
      available: boolean
    }>
    if (!isPlanningTruthAccepted(journal)) {
      return [purchaseRowBase]
    }
    const source = journal.summary.execution_by_flow
    if (source) {
      const productionAndRework = ['production', 'rework']
        .map((flow) => ({
          flow,
          label: flowLabel(flow),
          pct: source[flow]?.execution_pct ?? null,
          confirmedPct: source[flow]?.confirmed_pct ?? null,
          toOrderPct: null,
          base: source[flow]?.base_qty ?? 0,
          available: source[flow]?.available !== false,
        }))
      const purchase = {
        ...purchaseRowBase,
        pct: source.purchase?.covered_pct ?? null,
        toOrderPct: source.purchase?.to_order_pct ?? null,
        base: source.purchase?.base_qty ?? 0,
        available: source.purchase?.available !== false,
      }
      const knownFlows = new Set(['production', 'rework', 'purchase'])
      const extras = Object.keys(source)
        .filter((flow) => !knownFlows.has(flow))
        .map((flow) => ({
          flow,
          label: flowLabel(flow),
          pct: source[flow]?.execution_pct ?? null,
          confirmedPct: source[flow]?.confirmed_pct ?? null,
          toOrderPct: null,
          base: source[flow]?.base_qty ?? 0,
          available: source[flow]?.available !== false,
        }))
      return [...productionAndRework, purchase, ...extras].filter((row) => row.base > 1e-9 || row.flow === 'purchase' || !row.available)
    }
    const grouped = new Map<string, { completed: number; base: number }>()
    journal.rows.forEach((row) => {
      const base = row.progress_base_qty ?? row.net_qty ?? 0
      const entry = grouped.get(row.flow) ?? { completed: 0, base: 0 }
      entry.completed += row.completed_qty ?? 0
      entry.base += base
      grouped.set(row.flow, entry)
    })
    const productionAndRework = ['production', 'rework']
      .map((flow) => {
        const entry = grouped.get(flow)
        const base = entry?.base ?? 0
        const pct = base > 1e-9 ? Math.round(((entry?.completed ?? 0) / base) * 1000) / 10 : 100
        return { flow, label: flowLabel(flow), pct, confirmedPct: pct, toOrderPct: null, base, available: true }
      })
      .filter((row) => row.base > 1e-9)
    const purchase = {
      ...purchaseRowBase,
      base: journal.rows.some((row) => row.flow === 'purchase') ? 1 : 0,
    }
    return [...productionAndRework, purchase].filter((row) => row.base > 1e-9 || row.flow === 'purchase' || !row.available)
  }, [journal])

  const journalTruthAccepted = isPlanningTruthAccepted(journal)
  const journalTruthReason = journal?.truth_reason || journal?.reason

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

  function ledgerItemLink(row: Pick<ExecutionJournalRow, 'item_id' | 'item_code'>) {
    const itemId = Number(row.item_id) || null
    if (!itemId || !Number.isFinite(itemId)) return '#/ledger'
    return `#/ledger/items/${encodeURIComponent(String(itemId))}`
  }

  function ledgerReservationLink(row: Pick<ExecutionJournalRow, 'item_id' | 'ledger_links'>, reservationId: number) {
    const itemId = Number(row.ledger_links?.item_id ?? '')
    if (!Number.isFinite(itemId) || !itemId) return `#/ledger/items/${row.item_id}`
    return `#/ledger/items/${itemId}?tab=reservations&reservation_id=${encodeURIComponent(String(reservationId))}`
  }

  function ledgerEventLink(row: Pick<ExecutionJournalRow, 'item_id' | 'ledger_links'>, event: ExecutionJournalLedgerLinkEvent) {
    const reservationId = event.reservation_id
    const itemId = Number(row.ledger_links?.item_id ?? '')
    const base = Number.isFinite(itemId) && itemId ? `#/ledger/items/${itemId}` : '#/ledger'
    if (!Number.isFinite(reservationId) || !reservationId) return `${base}?tab=reservations`
    const search = new URLSearchParams({
      tab: 'reservations',
      reservation_id: String(reservationId),
      event_id: String(event.event_id),
    })
    return `${base}?${search.toString()}`
  }

  function ledgerLinksControls(row: ExecutionJournalRow) {
    const links: { href: string; label: string; title: string; kind: 'reservation' | 'event' }[] = []
    const ledgerLinks = row.ledger_links
    if (!ledgerLinks) return links
    const itemId = Number(ledgerLinks.item_id ?? '')
    const resolvedItemId = Number.isFinite(itemId) && itemId > 0 ? itemId : row.item_id
    const reservations = (ledgerLinks.reservation_ids ?? []).filter((reservationId): reservationId is number => Number.isFinite(reservationId) && reservationId > 0)
    if (reservations.length) {
      links.push({
        href: `#/ledger/items/${encodeURIComponent(String(resolvedItemId))}`,
        label: `Номенклатура #${resolvedItemId}`,
        title: 'Ledger номенклатуры',
        kind: 'reservation',
      })
      reservations.slice(0, 5).forEach((reservationId) => {
        links.push({
          href: ledgerReservationLink(row, reservationId),
          label: `Резерв #${reservationId}`,
          title: `Ledger: резерв #${reservationId}`,
          kind: 'reservation',
        })
      })
    }
    const events = (ledgerLinks.events ?? []).filter((event) => Number.isFinite(event.event_id) && event.event_id > 0)
    events.forEach((event) => {
      if (!event.reservation_id) return
      links.push({
        href: ledgerEventLink(row, event),
        label: `Событие #${event.event_id}`,
        title: `Ledger событие #${event.event_id}`,
        kind: 'event',
      })
    })
    return links
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
      <KeyboardShortcutShell shortcuts={shortcuts} />
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
            <button onClick={() => void handleAllocate()} disabled={acting || !activeRunId} title="Пересчитать остаточную потребность: добор недопокрытия и перепланировка ещё не открытых в 1С заказов от сегодня">Пересчёт</button>
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
                onBlur={() => {
                  if (searchBlurTimerRef.current) clearTimeout(searchBlurTimerRef.current)
                  searchBlurTimerRef.current = setTimeout(() => setSuggestOpen(false), 150)
                }}
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
              <button onClick={downloadJournalCsv} disabled={!journal || !journalTruthAccepted || journalLoading}>CSV</button>
              <div className="barSeparator" />
              <button
                onClick={() => void handleCreateProductionOrders()}
                disabled={acting || !journal || !journalTruthAccepted || !journal.rows.some((r) => r.flow === 'production' && r.remaining_qty > 1e-9)}
                title="Создать заказы производства для незакрытых строк производственного потока"
              >
                Создать заказы производства
              </button>
              {journal && (
                <>
                  <div className="barSeparator" />
                  {journalExecutionPct !== null && (
                    <span className="toolbarText" title="Выполнено / чистая потребность по всем строкам">Общее выполнение: {journalExecutionPct}%</span>
                  )}
                  {journalExecutionByFlow.map((row) => (
                    <span key={row.flow} className="toolbarText">
                      {row.label}:{' '}
                      {row.flow === 'purchase'
                        ? row.available && row.pct !== null && row.toOrderPct !== null
                          ? `покрыто ${row.pct}% · к заказу ${row.toOrderPct}%`
                          : 'недоступно'
                        : row.available && row.pct !== null
                          ? `${row.pct}%`
                          : row.confirmedPct !== null
                            ? `≥${row.confirmedPct}% · часть н/д`
                            : 'недоступно'}
                    </span>
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
                  {journal.summary.not_covered + journal.summary.partially_covered > 0 && (
                    <button
                      className="filterBtn"
                      style={{ color: 'var(--red)' }}
                      onClick={() => setJournalCoverage((v) => (v === 'incomplete' ? '' : 'incomplete'))}
                      title="Показать строки с неполным выполнением"
                    >
                      Невыполнено: {journal.summary.not_covered + journal.summary.partially_covered}
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

            {journal && !journalTruthAccepted && (
              <div
                role="status"
                style={{
                  padding: '10px 12px',
                  borderBottom: '1px solid #d6b45f',
                  background: '#fff4cf',
                  color: '#5f4700',
                  fontWeight: 700,
                }}
              >
                Исполнение не рассчитано/недоступно
                {journalTruthReason ? <span style={{ fontWeight: 400 }}> — {journalTruthReason}</span> : null}
              </div>
            )}

            {journalLoading && <div className="hintLine">Загрузка журнала…</div>}
            {journalError && <div className="errorLine">{journalError}</div>}
            {!journal && !journalLoading && !journalError && (
              <div className="emptyDetail" style={{ margin: 16 }}>
                MRP-снимок не создан. Зафиксируйте план и нажмите «MRP снимок».
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
                          <select value={journalCoverage} onChange={(e) => setJournalCoverage(e.target.value as JournalCoverageFilter)}>
                            <option value="">Все</option>
                            <option value="covered">Закрыто</option>
                            <option value="partial">Частично</option>
                            <option value="ordered">Оформлено</option>
                            <option value="none">Не оформлено</option>
                            <option value="incomplete">Невыполнено</option>
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
                    {filteredJournalRows.map((row) => {
                      const rowLedgerLinks = ledgerLinksControls(row)
                      return (
                      <React.Fragment key={row.req_id}>
                        <tr
                          className={expandedReq === row.req_id ? 'activeRow' : ''}
                          style={{ cursor: row.work_items.length ? 'pointer' : undefined }}
                          onClick={() => setExpandedReq(expandedReq === row.req_id ? null : row.req_id)}
                        >
                          <td><span className="muted">{row.item_article || row.item_code}</span></td>
                          <td>
                            <strong>
                              <a
                                href={ledgerItemLink(row)}
                                title={`Ledger номенклатуры: ${row.item_code}`}
                                onClick={(event) => event.stopPropagation()}
                                style={{ textDecoration: 'none' }}
                              >
                                {row.item_name}
                              </a>
                            </strong>
                            {row.item_article && <div className="muted">{row.item_article}</div>}
                          </td>
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
                          <td className="numCell">
                            {journalTruthAccepted && row.execution_available === false
                              ? <span className="muted" title={row.execution_unavailable_reason ?? undefined}>н/д</span>
                              : journalTruthAccepted && (row.completed_qty ?? 0) > 0
                                ? qty(row.completed_qty ?? 0)
                                : <span className="muted">—</span>}
                          </td>
                          <td className="numCell" style={{ color: row.remaining_qty > 0 ? 'var(--red)' : undefined }}>
                            {journalTruthAccepted && row.remaining_qty > 0 ? qty(row.remaining_qty) : '—'}
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            {row.need_date ? dateRu(row.need_date) : '—'}
                            <ForecastShift forecast={row} />
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            {journalTruthAccepted
                              ? <span className={`miniPill ${journalRowStatusClass(journalRowStatus(row))}`}>
                                  {journalRowStatusLabel(journalRowStatus(row))}
                                </span>
                              : <span className="muted">Недоступно</span>}
                          </td>
                          <td style={{ textAlign: 'center' }}>
                            {journalTruthAccepted && row.coverage_pct !== null
                              ? <span className={`miniPill ${coverageClass(row.coverage_pct)}`}>{row.coverage_pct}%</span>
                              : <span className="muted">—</span>}
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
                        {expandedReq === row.req_id && (
                          <tr>
                            <td />
                            <td colSpan={11} style={{ paddingLeft: 24, marginBottom: 6 }}>
                              {rowLedgerLinks.length > 0 ? (
                                <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                                  <span className="muted" style={{ fontSize: 12 }}>Ledger:</span>
                                  {rowLedgerLinks.map((link) => (
                                    <a
                                      key={link.label + link.href}
                                      href={link.href}
                                      onClick={(event) => event.stopPropagation()}
                                      className={`miniPill ${link.kind === 'event' ? 'partial' : 'to_move'}`}
                                      style={{ textDecoration: 'none' }}
                                      title={link.title}
                                    >
                                      {link.label}
                                    </a>
                                  ))}
                                </div>
                              ) : null}
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                      )
                    })}
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
