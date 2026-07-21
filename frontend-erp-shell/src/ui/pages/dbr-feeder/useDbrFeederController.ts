import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  DbrChainPreview,
  DbrFeederDeficit,
  DbrFeederDeficitsResult,
  DbrFeederPosition,
  DbrFeederPreview,
  DbrFeederSignal,
  DbrFeederSignalPreview,
  DbrKitLine,
  DbrLaunchConflictDetail,
  DbrProcessingBoard,
  DbrPurchaseLaunchResult,
  DbrSignalLaunchResult,
} from '../../../domain/dbr'
import {
  getDbrFeederDeficits,
  getDbrProcessingBoard,
  getDbrSettings,
  isDbrConflict,
  launchDbrPurchase,
  launchDbrSignal,
  listDbrFeederPositions,
  getDbrFeederSignal,
  listDbrFeederSignals,
  previewDbrFeederChain,
  previewDbrFeederPositions,
  previewDbrFeederSignals,
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
  const [signalsLoading, setSignalsLoading] = useState(false)
  const [expandedSignalId, setExpandedSignalId] = useState<number | null>(null)
  const [deficitFilter, setDeficitFilter] = useState('')
  const [chainEnabled, setChainEnabled] = useState(false)
  const [deficits, setDeficits] = useState<DbrFeederDeficitsResult | null>(null)
  const [deficitsLoading, setDeficitsLoading] = useState(false)
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
  const positionsLoadSequence = useRef(0)
  const signalsLoadSequence = useRef(0)
  const signalDetailLoadSequence = useRef(0)

  const load = useCallback(async (next: FeederFilters = applied) => {
    const sequence = ++positionsLoadSequence.current
    setLoading(true)
    setError('')
    try {
      const nextRows = await listDbrFeederPositions({
        active_only: true,
        search: next.search,
        zone: next.zone,
        mode: next.mode,
        supply: next.supply,
        limit: 5000,
      })
      if (sequence !== positionsLoadSequence.current) return
      setRows(nextRows)
    } catch (e) {
      if (sequence !== positionsLoadSequence.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (sequence === positionsLoadSequence.current) setLoading(false)
    }
  }, [applied])

  useEffect(() => { void load() }, [load])

  const loadSignals = useCallback(async (next: SignalFilters = appliedSignalFilters) => {
    const sequence = ++signalsLoadSequence.current
    setSignalsLoading(true)
    setError('')
    try {
      const nextSignals = await listDbrFeederSignals({ ...next, limit: 5000 })
      if (sequence !== signalsLoadSequence.current) return
      setSignals(nextSignals)
    } catch (e) {
      if (sequence !== signalsLoadSequence.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (sequence === signalsLoadSequence.current) setSignalsLoading(false)
    }
  }, [appliedSignalFilters])

  useEffect(() => { void loadSignals() }, [loadSignals])

  const loadDeficits = useCallback(async () => {
    setDeficitsLoading(true)
    try {
      setDeficits(await getDbrFeederDeficits())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeficitsLoading(false)
    }
  }, [])

  useEffect(() => { void loadDeficits() }, [loadDeficits])

  const loadProcessingBoard = useCallback(async () => {
    setProcessingLoading(true)
    try {
      setProcessingBoard(await getDbrProcessingBoard())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setProcessingLoading(false)
    }
  }, [])

  useEffect(() => { void loadProcessingBoard() }, [loadProcessingBoard])

  useEffect(() => {
    let cancelled = false
    void getDbrSettings()
      .then((settings) => { if (!cancelled) setChainEnabled(Boolean(settings.feeder_chain_enabled)) })
      .catch(() => { if (!cancelled) setChainEnabled(false) })
    return () => { cancelled = true }
  }, [])

  // Queue shown after the deficit drill-down: only signals blocked by the
  // clicked component keep visible when a deficit filter is active.
  const visibleSignals = useMemo(
    () => visibleFeederSignals(signals, deficitFilter),
    [signals, deficitFilter],
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

  const sortedDeficits = useMemo(
    () => sortFeederDeficits(deficits?.deficits ?? [], deficitSort),
    [deficits, deficitSort],
  )

  const summary = useMemo(() => summarizeFeederPositions(rows), [rows])

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
      await load(applied)
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
      await Promise.all([loadSignals(appliedSignalFilters), loadDeficits()])
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
      await Promise.all([loadSignals(appliedSignalFilters), loadDeficits()])
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

  async function selectSignal(signalId: number) {
    const sequence = ++signalDetailLoadSequence.current
    setSignalsLoading(true)
    setError('')
    try {
      const signal = await getDbrFeederSignal(signalId)
      if (sequence !== signalDetailLoadSequence.current) return
      setSelectedSignal(signal)
    } catch (e) {
      if (sequence !== signalDetailLoadSequence.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (sequence === signalDetailLoadSequence.current) setSignalsLoading(false)
    }
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
      await Promise.all([loadSignals(appliedSignalFilters), loadDeficits()])
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
      await Promise.all([loadSignals(appliedSignalFilters), loadDeficits()])
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
    rows, filters, setFilters, preview, setPreview, loading, saving, error, message,
    signals, signalFilters, setSignalFilters, setAppliedSignalFilters, signalPreview,
    setSignalPreview, selectedSignal, setSelectedSignal, signalsLoading, expandedSignalId,
    setExpandedSignalId, deficitFilter, setDeficitFilter, chainEnabled, deficits,
    deficitsLoading, deficitSort, setDeficitSort, chainPreview, setChainPreview,
    launchFlow, launchBusy, launchError, purchaseFlow, setPurchaseFlow, purchaseBusy, purchaseError,
    selectedPurchase, setSelectedPurchase, processingBoard, processingLoading,
    visibleSignals, purchaseSelectableIds, purchaseSelectedIds, allPurchaseSelected, sortedDeficits,
    summary, signalPreviewSummary, calculatePreview, rebuild, calculateSignalPreview,
    refreshSignals, calculateChainPreview, runChainRefresh, filterByDeficit, selectSignal,
    startLaunch, confirmLaunch, closeLaunch, togglePurchase, startPurchase, confirmPurchase,
    applyFilters, resetFilters, loadDeficits, loadProcessingBoard,
  }
}
