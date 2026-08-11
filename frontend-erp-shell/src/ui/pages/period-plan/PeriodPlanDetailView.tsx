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
import { searchNomenclature } from '../../../services/productionPlan'
import {
  addItemToPeriodPlan,
  bulkUpsertPeriodPlanLines,
  deleteItemFromPeriodPlan,
  fixPeriodPlan,
  getExecutionJournal,
  getPeriodPlanMatrix,
  listPeriodPlanRuns,
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

type JournalCoverageFilter = '' | 'incomplete' | 'covered' | 'partial' | 'ordered' | 'none' | 'net_zero' | 'execution_unavailable'

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

const JOURNAL_LIMIT = 100

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
  const [journalOffset, setJournalOffset] = useState(0)
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
      if (!selectedRunId && data.rows?.length) {
        setSelectedRunId(data.rows[0].run_id)
        setLastRunId((prev) => prev ?? data.rows[0].run_id)
      }
    } catch {
      // silent: empty plan with no runs is fine
    }
  }, [planId, selectedRunId])

  const loadJournal = useCallback(async (
    options: {
      run_id?: number | null
      flow?: string
      root_item_id?: number | null
      bom_level?: string
      status?: JournalCoverageFilter
      include_net_zero?: boolean
      sort_by?: JournalSortKey
      sort_dir?: SortDir
      offset?: number
    } = {},
  ) => {
    const nextRunId = options.run_id ?? activeRunId
    const hasRootItemFilter = Object.prototype.hasOwnProperty.call(options, 'root_item_id')
    const bomLevel = options.bom_level ?? journalBomLevel
    const status = options.status ?? journalCoverage
    const includeNetZero = options.include_net_zero ?? (journalShowNetZero || status === 'net_zero')
    setJournalLoading(true)
    setJournalError('')
    try {
      const data = await getExecutionJournal(planId, {
        flow: (options.flow ?? journalFlow) || undefined,
        run_id: nextRunId ?? undefined,
        root_item_id: hasRootItemFilter ? options.root_item_id : journalRootItemId,
        bom_level: bomLevel ? Number(bomLevel) : undefined,
        status: status === 'execution_unavailable' ? status : status || undefined,
        include_net_zero: includeNetZero,
        sort_by: options.sort_by ?? journalSortBy,
        sort_dir: options.sort_dir ?? journalSortDir,
        limit: JOURNAL_LIMIT,
        offset: options.offset ?? journalOffset,
      })
      setJournal(data)
      setJournalOffset(data.offset)
      setLastRunId(data.run_id)
      if (!selectedRunId) setSelectedRunId(data.run_id)
      setPlan(data.plan)
    } catch (e) {
      setJournalError(e instanceof Error ? e.message : String(e))
      setJournal(null)
    } finally {
      setJournalLoading(false)
    }
  }, [
    activeRunId,
    journalBomLevel,
    journalCoverage,
    journalFlow,
    journalRootItemId,
    journalShowNetZero,
    journalSortBy,
    journalSortDir,
    journalOffset,
    planId,
    selectedRunId,
  ])

  useEffect(() => { void loadMatrix() }, [loadMatrix])
  useEffect(() => { void loadRuns() }, [loadRuns])

  useEffect(() => {
    if (tab === 'matrix' && !matrix) void loadMatrix()
    if (tab === 'journal') {
      void loadJournal({ run_id: activeRunId ?? undefined })
    }
  }, [activeRunId, loadJournal, loadMatrix, matrix, tab])

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
        else void loadJournal({ run_id: activeRunId, offset: journalOffset })
        void loadRuns()
      },
    },
  ], [activeRunId, loadJournal, loadMatrix, loadRuns, journalOffset, onBack, tab])

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
    if (!confirm('Зафиксировать план? После фиксации редактирование уже введённых значений недоступно (можно только дозаполнять новые ячейки).')) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      await fixPeriodPlan(planId)
      setMessage('План зафиксирован.')
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
      if (item.item_id == null) {
        throw new Error(`У позиции «${item.item_name}» нет item_id — она не найдена в справочнике`)
      }
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
    const nextDir = journalSortBy === key && journalSortDir === 'asc' ? 'desc' : 'asc'
    setJournalSortBy(key)
    setJournalSortDir(nextDir)
    setJournalOffset(0)
  }
  function jSortArrow(key: JournalSortKey) {
    if (journalSortBy !== key) return ''
    return journalSortDir === 'asc' ? ' ▲' : ' ▼'
  }

  const journalRows = journal?.rows ?? []

  const journalOffsetPage = journal
    ? Math.max(1, Math.floor(journal.offset / Math.max(1, journal.limit)) + 1)
    : 1
  const journalTotalPages = journal
    ? Math.max(1, Math.ceil(journal.total / Math.max(1, journal.limit)))
    : 1
  const journalOffsetFrom = journal ? Math.min(journal.total, journal.offset + 1) : 0
  const journalOffsetTo = journal ? Math.min(journal.total, journal.offset + journal.limit) : 0
  const journalCanPrev = journalOffset > 0
  const journalCanNext = journal ? journal.offset + journal.limit < journal.total : false

  function jumpJournalOffset(nextOffset: number) {
    setJournalOffset(Math.max(0, nextOffset))
  }

  function setJournalFilters(next: {
    flow?: string
    root_item_id?: number | null
    bom_level?: string
    status?: JournalCoverageFilter
    include_net_zero?: boolean
  }) {
    if (typeof next.flow === 'string') setJournalFlow(next.flow)
    if ('root_item_id' in next) setJournalRootItemId(next.root_item_id ?? null)
    if (typeof next.bom_level === 'string') setJournalBomLevel(next.bom_level)
    if (typeof next.status === 'string') setJournalCoverage(next.status)
    if (typeof next.include_net_zero === 'boolean') setJournalShowNetZero(next.include_net_zero)
    setJournalOffset(0)
  }

  const bomLevels = useMemo(() => {
    if (!journal) return [] as number[]
    if (journal.facets?.bom_levels?.length) return [...new Set(journal.facets.bom_levels)].sort((a, b) => a - b)
    return Array.from(new Set(journal.rows.map((r) => r.bom_level))).sort((a, b) => a - b)
  }, [journal])

  const journalExecutionPct = useMemo(() => {
    if (!journal || !isPlanningTruthAccepted(journal)) return null
    if (typeof journal.summary.execution_pct === 'number') return journal.summary.execution_pct
    return null
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
          available: source[flow]?.available === true,
        }))
      const purchase = {
        ...purchaseRowBase,
        pct: source.purchase?.covered_pct ?? null,
        toOrderPct: source.purchase?.to_order_pct ?? null,
        base: source.purchase?.base_qty ?? 0,
        available: source.purchase?.available === true,
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
          available: source[flow]?.available === true,
        }))
      return [...productionAndRework, purchase, ...extras].filter((row) => row.base > 1e-9 || row.flow === 'purchase' || !row.available)
    }
    return ['production', 'rework', 'purchase'].map((flow) => ({
      flow,
      label: flowLabel(flow),
      pct: null,
      confirmedPct: null,
      toOrderPct: null,
      base: 0,
      available: false,
    }))
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
    const headers = ['Артикул', 'Номенклатура', 'Поток', 'Уровень', 'Потребность (брутто)', 'Чистая потребность', 'Оформлено', 'Не оформлено', 'Выполнено', 'Осталось выполнить', 'Срок', 'Статус', 'Выполнение %', 'Заданий']
    const esc = (v: unknown) => {
      const s = String(v ?? '')
      return /[",;\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
    }
    const body = journalRows.map((r) => [r.item_article || r.item_code, r.item_name, flowLabel(r.flow), r.bom_level, r.gross_qty, r.net_qty, r.ordered_qty, r.unassigned_qty ?? 0, r.completed_qty, r.remaining_qty, r.need_date ?? '', r.status_label || journalRowStatusLabel(journalRowStatus(r)), r.coverage_pct, r.work_items.length].map(esc).join(';'))
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

  const matrixBucketTotals = matrix?.bucket_totals ?? {}
  const matrixGrandTotal = matrix?.grand_total ?? matrix?.total_qty ?? 0

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
                          <strong>{qty(row.total_qty ?? 0)}</strong>
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
                      <td colSpan={2} style={{ textAlign: 'right', paddingRight: 6 }}>
                        Итого по неделям
                        <span style={{ color: 'var(--muted)', fontWeight: 400, marginLeft: 6 }}>
                          (серверно)
                        </span>
                      </td>
                      <td className="numCell">
                        <strong>{qty(matrixGrandTotal)}</strong>
                      </td>
                      {matrix.buckets.map((b) => (
                        <td key={b} className="numCell"><strong>{qty(matrixBucketTotals[b] ?? 0)}</strong></td>
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
              <button onClick={() => void loadJournal({ run_id: activeRunId ?? undefined, flow: journalFlow, root_item_id: journalRootItemId })} disabled={journalLoading}>Обновить</button>
              <button onClick={downloadJournalCsv} disabled={!journal || !journalTruthAccepted || journalLoading}>CSV</button>
              <div className="barSeparator" />
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
                    onClick={() => {
                      const status = journalCoverage === 'covered' ? '' : 'covered'
                      setJournalFilters({ status })
                    }}
                    title="Показать только закрытые строки"
                  >
                    Закрыто: {journal.summary.fully_covered} / {journal.summary.total_items}
                  </button>
                  {journal.summary.not_covered > 0 && (
                    <button
                      className="filterBtn"
                      style={{ color: 'var(--red)' }}
                      onClick={() => {
                        const status = journalCoverage === 'none' ? '' : 'none'
                        setJournalFilters({ status })
                      }}
                      title="Показать только строки без оформленных заказов"
                    >
                      Не начато: {journal.summary.not_covered}
                    </button>
                  )}
                  {journal.summary.partially_covered > 0 && (
                    <button
                      className="filterBtn"
                      style={{ color: 'var(--orange)' }}
                      onClick={() => {
                        const status = journalCoverage === 'partial' ? '' : 'partial'
                        setJournalFilters({ status })
                      }}
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
                        onChange={(e) => setJournalFilters({ include_net_zero: e.target.checked })}
                      />
                      Покрытые складом: {journal.summary.net_zero}
                    </label>
                  )}
                  <span className="toolbarText">Записей: {journalOffsetFrom} — {journalOffsetTo} из {journal.total}</span>
                  <span className="toolbarText">Стр. {journalOffsetPage} / {journalTotalPages}</span>
                  <button className="filterBtn" onClick={() => jumpJournalOffset(journalOffset - JOURNAL_LIMIT)} disabled={!journalCanPrev}>
                    ← Предыдущая
                  </button>
                  <button className="filterBtn" onClick={() => jumpJournalOffset(journalOffset + JOURNAL_LIMIT)} disabled={!journalCanNext}>
                    Следующая →
                  </button>
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
                          <select value={journalFlow} onChange={(e) => { setJournalFilters({ flow: e.target.value }) }}>
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
                          <select value={journalBomLevel} onChange={(e) => setJournalFilters({ bom_level: e.target.value })}>
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
                          <select value={journalCoverage} onChange={(e) => setJournalFilters({ status: e.target.value as JournalCoverageFilter })}>
                            <option value="">Все</option>
                            <option value="incomplete">Незавершённые</option>
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
                    {journalRows.map((row) => {
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
                            title={`Потребность с припусками: ${qty(row.gross_qty)} · Остаток принятого Ledger: ${row.stock_qty == null ? 'н/д' : qty(row.stock_qty)}`}
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
                          <td className="numCell" style={{ color: (row.remaining_qty ?? 0) > 0 ? 'var(--red)' : undefined }}>
                            {journalTruthAccepted && (row.remaining_qty ?? 0) > 0 ? qty(row.remaining_qty ?? 0) : '—'}
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
                              ? <span className={`miniPill ${journalRowStatusClass(journalRowStatus(row))}`}>{row.coverage_pct}%</span>
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
                                    {' '}· выполнено: <strong>{wi.completed_qty != null && wi.completed_qty > 0 ? qty(wi.completed_qty) : '—'}</strong>
                                    {' '}· осталось: <strong>{wi.remaining_qty != null ? qty(wi.remaining_qty) : '—'}</strong>
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
                    {!journalRows.length && (
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
          setJournalRootDialogOpen(false)
          setJournalFilters({ root_item_id: value })
        }}
        onClose={() => setJournalRootDialogOpen(false)}
      />
    </main>
  )
}
