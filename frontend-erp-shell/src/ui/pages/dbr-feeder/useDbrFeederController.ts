import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  DbrChainPreview,
  DbrFeederCockpitMeta,
  DbrFeederDeficit,
  DbrFeederDeficitsResult,
  DbrFeederPosition,
  DbrFeederPreview,
  DbrFeederSignal,
  DbrFeederSignalPreview,
  DbrKitLine,
  DbrLaunchConflictDetail,
  DbrProcessingBoard,
  DbrProcessingChainPreview,
  DbrProcessingOrderPreview,
  DbrProcessingTripManifest,
  DbrPurchaseLaunchResult,
  DbrSignalLaunchResult,
} from '../../../domain/dbr'
import {
  getDbrFeederCockpit,
  dbrSnapshotUnavailableMessage,
  getDbrProcessingTripManifest,
  getDbrProcessingTripManifestPrint,
  isDbrConflict,
  launchDbrPurchase,
  launchDbrSignal,
  previewDbrFeederChain,
  previewDbrFeederPositions,
  previewDbrFeederSignals,
  previewDbrProcessingChain,
  previewDbrProcessingOrder,
  rebuildDbrFeederPositions,
  refreshDbrFeederChain,
  refreshDbrFeederSignals,
} from '../../../services/dbr'
import {
  EMPTY_FILTERS,
  EMPTY_SIGNAL_FILTERS,
  purchaseSignalSelection,
  sortFeederDeficits,
  summarizeFeederPositions,
  summarizeSignalPreview,
  visibleFeederSignals,
} from './model'
import type { DeficitSortKey, FeederFilters, SignalFilters } from './model'

export function useDbrFeederController() {
  const [rows, setRows] = useState<DbrFeederPosition[]>([])
  const [cockpitMeta, setCockpitMeta] = useState<DbrFeederCockpitMeta | null>(null)
  const [filters, setFilters] = useState<FeederFilters>(EMPTY_FILTERS)
  const [applied, setApplied] = useState<FeederFilters>(EMPTY_FILTERS)
  const [preview, setPreview] = useState<DbrFeederPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [signals, setSignals] = useState<DbrFeederSignal[]>([])
  const [signalFilters, setSignalFilters] = useState<SignalFilters>(EMPTY_SIGNAL_FILTERS)
  const [appliedSignalFilters, setAppliedSignalFilters] = useState<SignalFilters>(EMPTY_SIGNAL_FILTERS)
  const [signalPreview, setSignalPreview] = useState<DbrFeederSignalPreview | null>(null)
  const [selectedSignal, setSelectedSignal] = useState<DbrFeederSignal | null>(null)
  const [signalsLoading] = useState(false)
  const [expandedSignalId, setExpandedSignalId] = useState<number | null>(null)
  const [deficitFilter, setDeficitFilter] = useState('')
  const [chainEnabled, setChainEnabled] = useState(false)
  const [deficits, setDeficits] = useState<DbrFeederDeficitsResult | null>(null)
  const [deficitsLoading] = useState(false)
  const [deficitSort, setDeficitSort] = useState<DeficitSortKey>('blocks_signals')
  const [chainPreview, setChainPreview] = useState<DbrChainPreview | null>(null)

  // Two-step launch of one make signal into a 1С production order.
  const [launchFlow, setLaunchFlow] = useState<{
    signal: DbrFeederSignal
    preview?: DbrSignalLaunchResult
    result?: DbrSignalLaunchResult
    deficit?: DbrKitLine[]
  } | null>(null)
  const [launchBusy, setLaunchBusy] = useState(false)
  const [launchError, setLaunchError] = useState('')

  // Mass supplier order for selected «Пополнение» signals.
  const [selectedPurchase, setSelectedPurchase] = useState<Set<number>>(new Set())
  const [purchaseFlow, setPurchaseFlow] = useState<{
    signalIds?: number[]
    preview?: DbrPurchaseLaunchResult
    result?: DbrPurchaseLaunchResult
  } | null>(null)
  const [purchaseBusy, setPurchaseBusy] = useState(false)
  const [purchaseError, setPurchaseError] = useState('')
  const [processingBoard, setProcessingBoard] = useState<DbrProcessingBoard | null>(null)
  const [processingLoading, setProcessingLoading] = useState(false)
  const [processingChainPreview, setProcessingChainPreview] = useState<DbrProcessingChainPreview | null>(null)
  const [processingOrderPreview, setProcessingOrderPreview] = useState<DbrProcessingOrderPreview | null>(null)
  const [processingManifest, setProcessingManifest] = useState<DbrProcessingTripManifest | null>(null)
  const cockpitLoadSequence = useRef(0)

  // The initial mount is intentionally a single GET.  Filtering is local over
  // this immutable saved envelope; it must never re-run DBR calculations.
  const loadCockpit = useCallback(async () => {
    const sequence = ++cockpitLoadSequence.current
    setLoading(true)
    setError('')
    try {
      const cockpit = await getDbrFeederCockpit()
      if (sequence !== cockpitLoadSequence.current) return
      setRows(cockpit.positions ?? [])
      setSignals(cockpit.signals ?? [])
      // Candidate snapshots intentionally expose only obligation-level deficit
      // rows.  They are not the material-readiness table, so do not turn their
      // absent fields into deceptive zeroes.
      const deficitPayload = cockpit.deficits
      setDeficits(deficitPayload && 'deficits' in deficitPayload ? deficitPayload : null)
      const processingPayload = cockpit.processing_board
      setProcessingBoard(processingPayload && 'positions' in processingPayload ? processingPayload : null)
      setCockpitMeta(cockpit.meta ?? {})
      setChainEnabled(Boolean(cockpit.meta?.chain_enabled))
    } catch (e) {
      if (sequence !== cockpitLoadSequence.current) return
      setError(dbrSnapshotUnavailableMessage(e) ?? (e instanceof Error ? e.message : String(e)))
    } finally {
      if (sequence === cockpitLoadSequence.current) setLoading(false)
    }
  }, [])

  useEffect(() => { void loadCockpit() }, [loadCockpit])

  const loadDeficits = loadCockpit
  const loadProcessingBoard = loadCockpit

  async function calculateProcessingChainPreview() {
    setProcessingLoading(true)
    setError('')
    try {
      setProcessingChainPreview(await previewDbrProcessingChain())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setProcessingLoading(false)
    }
  }

  async function calculateProcessingOrderPreview(signalId: number) {
    setProcessingLoading(true)
    setError('')
    try {
      setProcessingOrderPreview(await previewDbrProcessingOrder(signalId))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setProcessingLoading(false)
    }
  }

  async function loadProcessingManifest() {
    setProcessingLoading(true)
    setError('')
    try {
      setProcessingManifest(await getDbrProcessingTripManifest())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setProcessingLoading(false)
    }
  }

  async function printProcessingManifest() {
    setProcessingLoading(true)
    setError('')
    try {
      const html = await getDbrProcessingTripManifestPrint()
      const url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }))
      window.open(url, '_blank', 'noopener,noreferrer')
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setProcessingLoading(false)
    }
  }

  const sortedDeficits = useMemo(
    () => sortFeederDeficits(deficits?.deficits ?? [], deficitSort),
    [deficits, deficitSort],
  )

  const filteredRows = useMemo(() => rows.filter((row) => {
    const search = applied.search.trim().toLocaleLowerCase()
    const zone = row.live_nfp?.zone ?? ''
    return row.is_active
      && (!search || `${row.item_code} ${row.item_name}`.toLocaleLowerCase().includes(search))
      && (!applied.zone || zone === applied.zone)
      && (!applied.mode || row.mode === applied.mode)
      && (!applied.supply || row.supply_type === applied.supply)
  }), [applied, rows])

  const filteredSignals = useMemo(() => signals.filter((signal) => {
    const search = appliedSignalFilters.search.trim().toLocaleLowerCase()
    return (!search || `${signal.item_code ?? ''} ${signal.item_name ?? ''}`.toLocaleLowerCase().includes(search))
      && (!appliedSignalFilters.status || signal.status === appliedSignalFilters.status)
      && (!appliedSignalFilters.zone || signal.zone === appliedSignalFilters.zone)
      && (!appliedSignalFilters.signal_type || signal.signal_type === appliedSignalFilters.signal_type)
  }), [appliedSignalFilters, signals])

  const visibleSignals = useMemo(
    () => visibleFeederSignals(filteredSignals, deficitFilter),
    [filteredSignals, deficitFilter],
  )

  // «Пополнение» signals are the ones the supplier-order launch can target; only
  // open ones are checkbox-selectable.
  const purchaseSelection = useMemo(
    () => purchaseSignalSelection(visibleSignals, selectedPurchase),
    [visibleSignals, selectedPurchase],
  )
  const purchaseSelectableIds = purchaseSelection.selectableIds
  const purchaseSelectedIds = purchaseSelection.selectedIds
  const allPurchaseSelected = purchaseSelection.allSelected

  const summary = useMemo(() => summarizeFeederPositions(filteredRows), [filteredRows])

  const unavailableSections = useMemo(() => {
    const raw = cockpitMeta?.unavailable_sections
    if (Array.isArray(raw)) return Object.fromEntries(raw.map((section) => [section, 'Недоступно в сохранённом снимке']))
    const sections: Record<string, string | null | undefined> = { ...(raw ?? {}) }
    if (cockpitMeta && !deficits) sections.deficits ??= 'В снимке есть только дефицит открытых обязательств, без готовности комплектов'
    if (cockpitMeta && !processingBoard) sections.processing_board ??= 'Точный контур переработки не зафиксирован в этом снимке'
    return sections
  }, [cockpitMeta, deficits, processingBoard])
  const sectionUnavailableReason = useCallback(
    (section: 'positions' | 'signals' | 'deficits' | 'processing_board') => {
      const reason = unavailableSections[section]
      return typeof reason === 'string' ? reason : null
    },
    [unavailableSections],
  )

  const signalPreviewSummary = useMemo(() => summarizeSignalPreview(signalPreview), [signalPreview])

  async function calculatePreview() {
    setSaving(true)
    setError('')
    setMessage('')
    setPreview(null)
    try {
      setPreview(await previewDbrFeederPositions())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function rebuild() {
    if (!preview) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const result = await rebuildDbrFeederPositions(preview.schedule_id)
      setPreview(null)
      setMessage(`Позиции обновлены по графику №${result.schedule_id}: создано ${result.created ?? 0}, обновлено ${result.updated ?? 0}, отключено ${result.deactivated ?? 0}`)
      await loadCockpit()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function calculateSignalPreview() {
    setSaving(true)
    setError('')
    setMessage('')
    setSignalPreview(null)
    try {
      setSignalPreview(await previewDbrFeederSignals())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function refreshSignals() {
    if (!signalPreview?.schedule_id) {
      setError('Нельзя обновить сигналы без активного графика')
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const result = await refreshDbrFeederSignals(signalPreview.schedule_id)
      setSignalPreview(null)
      setSelectedSignal(null)
      setMessage(`Advisory-очередь обновлена по графику №${result.schedule_id ?? 'нет'}: создано ${result.created ?? 0}, обновлено ${result.updated ?? 0}, переоткрыто ${result.reopened ?? 0}, отменено ${result.cancelled ?? 0}`)
      await loadCockpit()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function calculateChainPreview() {
    setSaving(true)
    setError('')
    setMessage('')
    setChainPreview(null)
    try {
      setChainPreview(await previewDbrFeederChain())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function runChainRefresh() {
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const result = await refreshDbrFeederChain()
      setChainPreview(null)
      if (result.disabled) {
        setMessage('Цепочка отключена в настройках DBR — обновление не выполнено.')
      } else {
        setMessage(`Цепочка обновлена: создано ${result.created}, обновлено ${result.updated}, переоткрыто ${result.reopened}, отозвано ${result.revoked}, проходов ${result.passes}${result.no_warehouse ? `, без склада ${result.no_warehouse}` : ''}`)
      }
      await loadCockpit()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function filterByDeficit(deficit: DbrFeederDeficit) {
    setDeficitFilter((current) => (current === deficit.item ? '' : deficit.item))
    setExpandedSignalId(null)
    setSelectedSignal(null)
  }

  function selectSignal(signalId: number) {
    // The detail is already in the same immutable cockpit snapshot.  A second
    // GET here could mix it with another Ledger generation.
    setSelectedSignal(signals.find((signal) => signal.id === signalId) ?? null)
  }

  // ── Launch one signal into a 1С production order (preview → confirm) ────────
  async function startLaunch(signal: DbrFeederSignal) {
    setLaunchFlow({ signal })
    setLaunchBusy(true)
    setLaunchError('')
    try {
      // The dry-run already runs the material gate, so a deficit 409s here.
      const preview = await launchDbrSignal(signal.id, true)
      setLaunchFlow({ signal, preview })
    } catch (e) {
      if (isDbrConflict(e)) {
        const detail = e.detail as DbrLaunchConflictDetail | undefined
        setLaunchFlow({ signal, deficit: detail?.deficit_lines ?? [] })
        setLaunchError(e.message)
      } else {
        setLaunchFlow(null)
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setLaunchBusy(false)
    }
  }

  async function confirmLaunch() {
    if (!launchFlow?.signal) return
    setLaunchBusy(true)
    setLaunchError('')
    try {
      const result = await launchDbrSignal(launchFlow.signal.id, false)
      setLaunchFlow((prev) => (prev ? { ...prev, result } : prev))
      await loadCockpit()
    } catch (e) {
      if (isDbrConflict(e)) {
        const detail = e.detail as DbrLaunchConflictDetail | undefined
        setLaunchFlow((prev) => (prev ? { ...prev, deficit: detail?.deficit_lines ?? [] } : prev))
      }
      setLaunchError(e instanceof Error ? e.message : String(e))
    } finally {
      setLaunchBusy(false)
    }
  }

  function closeLaunch() {
    setLaunchFlow(null)
    setLaunchError('')
  }

  // ── Mass supplier order (preview → confirm) ─────────────────────────────────
  function togglePurchase(signalId: number) {
    setSelectedPurchase((prev) => {
      const next = new Set(prev)
      if (next.has(signalId)) next.delete(signalId)
      else next.add(signalId)
      return next
    })
  }

  async function startPurchase(ids?: number[]) {
    const selected = ids && ids.length ? ids : undefined
    setPurchaseFlow({ signalIds: selected })
    setPurchaseBusy(true)
    setPurchaseError('')
    try {
      const preview = await launchDbrPurchase(selected, true)
      setPurchaseFlow({ signalIds: selected, preview })
    } catch (e) {
      setPurchaseFlow(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPurchaseBusy(false)
    }
  }

  async function confirmPurchase() {
    if (!purchaseFlow) return
    setPurchaseBusy(true)
    setPurchaseError('')
    try {
      const result = await launchDbrPurchase(purchaseFlow.signalIds, false)
      setPurchaseFlow((prev) => (prev ? { ...prev, result } : prev))
      setSelectedPurchase(new Set())
      await loadCockpit()
    } catch (e) {
      setPurchaseError(e instanceof Error ? e.message : String(e))
    } finally {
      setPurchaseBusy(false)
    }
  }

  function applyFilters() {
    setApplied(filters)
  }

  function resetFilters() {
    setFilters(EMPTY_FILTERS)
    setApplied(EMPTY_FILTERS)
  }

  return {
    rows: filteredRows, cockpitMeta, unavailableSections, sectionUnavailableReason,
    filters, setFilters, preview, setPreview, loading, saving, error, message,
    signals, signalFilters, setSignalFilters, setAppliedSignalFilters, signalPreview,
    setSignalPreview, selectedSignal, setSelectedSignal, signalsLoading, expandedSignalId,
    setExpandedSignalId, deficitFilter, setDeficitFilter, chainEnabled, deficits,
    deficitsLoading, deficitSort, setDeficitSort, chainPreview, setChainPreview,
    launchFlow, launchBusy, launchError, purchaseFlow, setPurchaseFlow, purchaseBusy, purchaseError,
    selectedPurchase, setSelectedPurchase, processingBoard, processingLoading,
    processingChainPreview, setProcessingChainPreview, processingOrderPreview, setProcessingOrderPreview,
    processingManifest, setProcessingManifest,
    visibleSignals, purchaseSelectableIds, purchaseSelectedIds, allPurchaseSelected, sortedDeficits,
    summary, signalPreviewSummary, calculatePreview, rebuild, calculateSignalPreview,
    refreshSignals, calculateChainPreview, runChainRefresh, filterByDeficit, selectSignal,
    startLaunch, confirmLaunch, closeLaunch, togglePurchase, startPurchase, confirmPurchase,
    applyFilters, resetFilters, loadDeficits, loadProcessingBoard, loadCockpit,
    calculateProcessingChainPreview, calculateProcessingOrderPreview, loadProcessingManifest,
    printProcessingManifest,
  }
}
