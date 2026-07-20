import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
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
} from '../../domain/dbr'
import { ApiError } from '../../lib/api'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import {
  getDbrFeederDeficits,
  getDbrProcessingBoard,
  getDbrSettings,
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
} from '../../services/dbr'
import { DbrConfirmDialog } from '../dbr/DbrConfirmDialog'
import { DbrNav } from '../dbr/DbrNav'
import { DbrPurchaseResultBody } from '../dbr/DbrPurchaseResultBody'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const ZONE_LABEL: Record<string, string> = { green: 'Зелёная', yellow: 'Жёлтая', red: 'Красная' }
const MODE_LABEL: Record<string, string> = { shelf: 'Полка', under_schedule: 'Под график' }
const SUPPLY_LABEL: Record<string, string> = { purchase: 'Закупка', manufacture: 'Производство', processing: 'Переработка' }
const REASON_LABEL: Record<string, string> = {
  open_supply_destination_missing: 'не указан склад открытого прихода',
  stale_schedule: 'позиция рассчитана не по активному графику',
  production_inbound_destination_missing: 'не указан склад производственного прихода',
  production_inbound_eta_missing: 'не указана дата производственного прихода',
  supplier_inbound_destination_missing: 'не указан склад прихода поставщика',
  supplier_inbound_eta_missing: 'не указана дата прихода поставщика',
}
const SIGNAL_TYPE_LABEL: Record<string, string> = { 'Пополнение': 'Пополнение', 'Под график': 'Под график' }
// Material readiness: kit_cls → badge colour class (green/yellow/red/gray).
const MATERIAL_CLASS: Record<string, string> = { ok: 'green', part: 'yellow', no: 'red', q: 'gray' }
const MATERIAL_TITLE: Record<string, string> = {
  'Готов': 'Комплект обеспечен запасом предприятия',
  'Частично': 'Обеспечена часть комплекта',
  'Дефицит': 'Не хватает материала на комплект',
  'Расписан выше': 'Материал забран сигналами выше по очереди',
}
const SOURCE_LABEL: Record<string, string> = { make: 'Производство', buy: 'Закупка' }
type DeficitSortKey = 'blocks_signals' | 'short_qty' | 'nearest_due' | 'item'

type Filters = { search: string; zone: string; mode: string; supply: string }
const EMPTY_FILTERS: Filters = { search: '', zone: '', mode: '', supply: '' }
type SignalFilters = { search: string; zone: string; status: string; signal_type: string }
const EMPTY_SIGNAL_FILTERS: SignalFilters = { search: '', zone: '', status: 'Open', signal_type: '' }

function zoneKey(value?: string | null) {
  return String(value ?? 'unknown').trim().toLowerCase()
}

export function DbrFeederPage() {
  const [rows, setRows] = useState<DbrFeederPosition[]>([])
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS)
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

  const load = useCallback(async (next: Filters = applied) => {
    setLoading(true)
    setError('')
    try {
      setRows(await listDbrFeederPositions({
        active_only: true,
        search: next.search,
        zone: next.zone,
        mode: next.mode,
        supply: next.supply,
        limit: 5000,
      }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [applied])

  useEffect(() => { void load() }, [load])

  const loadSignals = useCallback(async (next: SignalFilters = appliedSignalFilters) => {
    setSignalsLoading(true)
    setError('')
    try {
      setSignals(await listDbrFeederSignals({ ...next, limit: 5000 }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSignalsLoading(false)
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
  const visibleSignals = useMemo(() => {
    if (!deficitFilter) return signals
    return signals.filter((signal) => (signal.deficit_lines ?? []).some((line) => line.item === deficitFilter))
  }, [signals, deficitFilter])

  // «Пополнение» signals are the ones the supplier-order launch can target; only
  // open ones are checkbox-selectable.
  const purchaseSelectableIds = useMemo(
    () => visibleSignals.filter((s) => s.signal_type === 'Пополнение' && s.status === 'Open').map((s) => s.id),
    [visibleSignals],
  )
  const purchaseSelectedIds = useMemo(
    () => purchaseSelectableIds.filter((id) => selectedPurchase.has(id)),
    [purchaseSelectableIds, selectedPurchase],
  )
  const allPurchaseSelected = purchaseSelectableIds.length > 0 && purchaseSelectedIds.length === purchaseSelectableIds.length

  const sortedDeficits = useMemo(() => {
    const list = [...(deficits?.deficits ?? [])]
    list.sort((a, b) => {
      switch (deficitSort) {
        case 'short_qty': return b.short_qty - a.short_qty
        case 'nearest_due': return (a.nearest_due || '9999') < (b.nearest_due || '9999') ? -1 : 1
        case 'item': return a.item.localeCompare(b.item)
        case 'blocks_signals':
        default: return b.blocks_signals - a.blocks_signals
      }
    })
    return list
  }, [deficits, deficitSort])

  const summary = useMemo(() => rows.reduce((acc, row) => {
    const zone = zoneKey(row.live_nfp?.zone)
    acc[zone] = (acc[zone] ?? 0) + 1
    if (!row.live_nfp?.is_complete) acc.incomplete = (acc.incomplete ?? 0) + 1
    return acc
  }, {} as Record<string, number>), [rows])

  const signalPreviewSummary = useMemo(() => {
    const actionable = signalPreview?.rows.filter((row) => row.action === 'open' || row.action === 'update') ?? []
    return {
      replenish: actionable.filter((row) => row.signal_type === 'Пополнение').length,
      underSchedule: actionable.filter((row) => row.signal_type === 'Под график').length,
    }
  }, [signalPreview])

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
    setSignalsLoading(true)
    setError('')
    try {
      setSelectedSignal(await getDbrFeederSignal(signalId))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSignalsLoading(false)
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
      if (e instanceof ApiError && e.status === 409) {
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
      if (e instanceof ApiError && e.status === 409) {
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

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование DBR / Питающий контур</div>
        <div className="runBadge">Только контроль запасов</div>
      </div>

      <DocumentWindow
        title="Позиции супермаркета"
        subtitle="Статические буферы и живой NFP: остаток + открытый приход − квалифицированный спрос"
        hotkeys="F5 Обновить"
        footer={<StatusBar loading={loading} visibleFrom={rows.length ? 1 : 0} visibleTo={rows.length} total={rows.length} selectedCount={0} canPrev={false} canNext={false} onPrev={() => undefined} onNext={() => undefined} />}
      >
        <DbrNav />

        <div className="commandBar dbrFeederBar">
          <input
            className="dbrFeederSearch"
            value={filters.search}
            placeholder="Код или наименование"
            onChange={(e) => setFilters({ ...filters, search: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Enter') applyFilters() }}
          />
          <select aria-label="Зона NFP" value={filters.zone} onChange={(e) => setFilters({ ...filters, zone: e.target.value })}>
            <option value="">Все зоны</option><option value="red">Красная</option><option value="yellow">Жёлтая</option><option value="green">Зелёная</option>
          </select>
          <select aria-label="Режим позиции" value={filters.mode} onChange={(e) => setFilters({ ...filters, mode: e.target.value })}>
            <option value="">Все режимы</option><option value="shelf">Полка</option><option value="under_schedule">Под график</option>
          </select>
          <select aria-label="Тип снабжения" value={filters.supply} onChange={(e) => setFilters({ ...filters, supply: e.target.value })}>
            <option value="">Все типы</option><option value="purchase">Закупка</option><option value="manufacture">Производство</option><option value="processing">Переработка</option>
          </select>
          <button onClick={applyFilters} disabled={loading}>Применить</button>
          <button onClick={resetFilters} disabled={loading}>Сбросить</button>
          <div className="commandBarSpacer" />
          <button onClick={() => void calculatePreview()} disabled={saving}>Предпросмотр пересчёта</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}
        <div className="dbrFeederNotice">Пересчёт позиций и предпросмотр сигналов — только чтение. Запуск сигнала и заказ поставщику создают документы в живой 1С и всегда требуют подтверждения в отдельном окне.</div>

        {preview && (
          <section className="dbrFeederPreview" aria-label="Предпросмотр пересчёта">
            <div>
              <strong>График №{preview.schedule_id}: {preview.positions.length} позиций</strong>
              <span>{preview.warnings.length ? `Предупреждений качества: ${preview.warnings.length}` : 'Предупреждений качества нет'}</span>
              {!!preview.warnings.length && <details><summary>Показать предупреждения</summary><ul>{preview.warnings.slice(0, 100).map((warning) => <li key={warning}>{warning}</li>)}</ul></details>}
            </div>
            <div className="dbrFeederPreviewActions">
              <button onClick={() => setPreview(null)} disabled={saving}>Отмена</button>
              <button className="primary" onClick={() => void rebuild()} disabled={saving}>Перестроить по графику №{preview.schedule_id}</button>
            </div>
          </section>
        )}

        <section className="dbrSignalSection" aria-label="Advisory-сигналы питающего контура">
          <div className="dbrSignalHeader">
            <div>
              <h2>Advisory-очередь питающего контура</h2>
              <p>«Пополнение» управляет полкой, «Под график» показывает дефицит к конкретному слоту. Отрицательный приоритет означает, что срок запуска ещё не наступил.</p>
            </div>
            <div className="dbrSignalHeaderActions">
              <button onClick={() => void calculateSignalPreview()} disabled={saving}>Предпросмотр сигналов</button>
              <button
                className="dbrDanger"
                onClick={() => void startPurchase(purchaseSelectedIds)}
                disabled={saving || purchaseBusy}
                title="Создать заказы поставщику по выбранным сигналам «Пополнение» (или по всем открытым закупочным, если ничего не выбрано)"
              >
                Заказать поставщику…{purchaseSelectedIds.length ? ` (${purchaseSelectedIds.length})` : ''}
              </button>
              {chainEnabled && <>
                <button onClick={() => void calculateChainPreview()} disabled={saving}>Цепочка: предпросмотр</button>
                <button onClick={() => void runChainRefresh()} disabled={saving}>Цепочка: обновить</button>
              </>}
            </div>
          </div>

          {signalPreview && (
            <div className="dbrFeederPreview dbrSignalPreview" aria-label="Предпросмотр обновления сигналов">
              <div>
                <strong>График №{signalPreview.schedule_id ?? 'не активен'}: {signalPreview.actionable} актуальных сигналов</strong>
                <span>Пополнение: {signalPreviewSummary.replenish}; под график: {signalPreviewSummary.underSchedule}</span>
                <span>Позиций проверено: {signalPreview.positions}; открыть: {signalPreview.rows.filter((row) => row.action === 'open').length}, обновить: {signalPreview.rows.filter((row) => row.action === 'update').length}, диагностика: {signalPreview.diagnostic ?? 0}, отменить: {signalPreview.rows.filter((row) => row.action === 'cancel').length}</span>
                <span>Это обновит только advisory-проекцию DBR.</span>
              </div>
              <div className="dbrFeederPreviewActions">
                <button onClick={() => setSignalPreview(null)} disabled={saving}>Отмена</button>
                <button className="primary" onClick={() => void refreshSignals()} disabled={saving || !signalPreview.schedule_id}>Обновить по графику №{signalPreview.schedule_id ?? 'нет'}</button>
              </div>
            </div>
          )}

          <div className="commandBar dbrFeederBar dbrSignalFilters">
            <input className="dbrFeederSearch" value={signalFilters.search} placeholder="Сигнал: код или наименование" onChange={(e) => setSignalFilters({ ...signalFilters, search: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter') setAppliedSignalFilters(signalFilters) }} />
            <select aria-label="Статус сигнала" value={signalFilters.status} onChange={(e) => setSignalFilters({ ...signalFilters, status: e.target.value })}>
              <option value="Open">Открытые</option><option value="Diagnostic">Диагностические</option><option value="Cancelled">Отменённые</option><option value="">Все статусы</option>
            </select>
            <select aria-label="Зона сигнала" value={signalFilters.zone} onChange={(e) => setSignalFilters({ ...signalFilters, zone: e.target.value })}>
              <option value="">Все зоны</option><option value="red">Красная</option><option value="yellow">Жёлтая</option><option value="green">Зелёная</option>
            </select>
            <select aria-label="Тип сигнала" value={signalFilters.signal_type} onChange={(e) => setSignalFilters({ ...signalFilters, signal_type: e.target.value })}>
              <option value="">Все типы</option><option value="Пополнение">Пополнение</option><option value="Под график">Под график</option>
            </select>
            <button onClick={() => setAppliedSignalFilters(signalFilters)} disabled={signalsLoading}>Применить</button>
            <button onClick={() => { setSignalFilters(EMPTY_SIGNAL_FILTERS); setAppliedSignalFilters(EMPTY_SIGNAL_FILTERS) }} disabled={signalsLoading}>Сбросить</button>
            {deficitFilter && (
              <button className="dbrDeficitChip" onClick={() => setDeficitFilter('')} title="Сбросить фильтр по дефициту">
                Дефицит: {deficitFilter} ✕
              </button>
            )}
            <div className="commandBarSpacer" />
            <span className="dbrSignalCount">Сигналов: {visibleSignals.length}{deficitFilter ? ` из ${signals.length}` : ''}</span>
          </div>

          <div className="dbrSignalLayout">
            <div className="dbrFeederTableWrap">
              <table className="journalTable dbrTable dbrSignalTable">
                <thead><tr><th className="dbrCheckCell"><input type="checkbox" aria-label="Выбрать все закупочные сигналы" checked={allPurchaseSelected} disabled={!purchaseSelectableIds.length} onChange={(e) => setSelectedPurchase(e.target.checked ? new Set(purchaseSelectableIds) : new Set())} /></th><th aria-label="Раскрытие" /><th>Тип</th><th>Материал</th><th>KIT</th><th>Приоритет</th><th>Зона</th><th>Номенклатура</th><th>Склад</th><th>Крайний срок запуска</th><th>Дата потребности / слота</th><th className="numCell">Спрос</th><th className="numCell">Дефицит</th><th className="numCell">Количество</th><th className="numCell">Расчётная партия</th><th>Слот</th><th>Качество</th><th>Статус</th><th>Обновлён</th><th aria-label="Действие" /></tr></thead>
                <tbody>
                  {!signalsLoading && !visibleSignals.length && <tr><td colSpan={20} className="emptyCell">{deficitFilter ? 'Нет сигналов, заблокированных этой позицией.' : 'Сигналы не найдены. Выполните предпросмотр и явное обновление.'}</td></tr>}
                  {visibleSignals.map((signal) => {
                    const normalizedZone = zoneKey(signal.zone)
                    const deficitLines = signal.deficit_lines ?? []
                    const hasDeficit = deficitLines.length > 0
                    const isExpanded = expandedSignalId === signal.id
                    const matCls = signal.kit_cls ? MATERIAL_CLASS[signal.kit_cls] ?? 'gray' : ''
                    const chainDepth = signal.chain_depth ?? 0
                    return (
                      <Fragment key={signal.id}>
                      <tr className={`${selectedSignal?.id === signal.id ? 'selected' : ''} ${signal.kit_force ? 'dbrSignalKitRow' : ''} ${signal.is_incomplete ? 'dbrFeederIncomplete' : ''}`} onClick={() => void selectSignal(signal.id)}>
                        <td className="dbrCheckCell" onClick={(e) => e.stopPropagation()}>
                          {signal.signal_type === 'Пополнение' && signal.status === 'Open' && (
                            <input type="checkbox" aria-label={`Выбрать сигнал ${signal.item_code ?? signal.id} для заказа поставщику`} checked={selectedPurchase.has(signal.id)} onChange={() => togglePurchase(signal.id)} />
                          )}
                        </td>
                        <td className="dbrExpandCell">
                          {hasDeficit
                            ? <button className={`dbrExpandBtn ${isExpanded ? 'open' : ''}`} aria-label={isExpanded ? 'Свернуть дефицит' : 'Показать дефицит'} aria-expanded={isExpanded} onClick={(e) => { e.stopPropagation(); setExpandedSignalId(isExpanded ? null : signal.id) }}>{isExpanded ? '▾' : '▸'}</button>
                            : null}
                        </td>
                        <td>
                          <span className={`dbrSignalTypeBadge ${signal.signal_type === 'Под график' ? 'schedule' : signal.signal_type === 'Цепочка' ? 'chain' : 'replenish'}`}>{SIGNAL_TYPE_LABEL[signal.signal_type] ?? signal.signal_type}</span>
                          {chainDepth > 0 && (
                            <span className="dbrChainMark" title={`Цепочка, уровень ${chainDepth}`}>
                              ⤷ цепочка
                              {signal.parent_signal_id != null && (
                                <button className="dbrChainParent" onClick={(e) => { e.stopPropagation(); void selectSignal(signal.parent_signal_id as number) }} title="Открыть родительский сигнал">→ #{signal.parent_signal_id}</button>
                              )}
                            </span>
                          )}
                        </td>
                        <td>{signal.material_status ? <span className={`dbrMatBadge ${matCls}`} title={MATERIAL_TITLE[signal.material_status] ?? signal.material_status}><span className={`dbrDot ${matCls === 'green' ? 'g' : matCls === 'yellow' ? 'y' : matCls === 'red' ? 'r' : 'n'}`} />{signal.material_status}</span> : '—'}</td>
                        <td>{signal.kit_force ? <span className="dbrKitForce">KIT</span> : '—'}</td>
                        <td className="numCell"><strong>{Number(signal.priority).toFixed(2)}</strong></td>
                        <td><span className={`dbrZoneBadge ${normalizedZone}`}><span className={`dbrDot ${normalizedZone.slice(0, 1)}`} />{ZONE_LABEL[normalizedZone] ?? signal.zone}</span></td>
                        <td className={chainDepth > 0 ? 'dbrChainIndent' : undefined}><strong>{signal.item_code ?? `#${signal.item_id}`}</strong><span className="dbrFeederItemName">{signal.item_name}</span></td>
                        <td title={signal.warehouse_ref1c}>{signal.warehouse_ref1c}</td>
                        <td>{signal.signal_type === 'Под график' ? dateRu(signal.need_date) || '—' : '—'}</td>
                        <td>{signal.signal_type === 'Под график' ? dateRu(signal.required_date) || '—' : '—'}</td>
                        <td className="numCell">{signal.signal_type === 'Под график' ? qty(signal.raw_demand_qty) : '—'}</td>
                        <td className="numCell">{signal.signal_type === 'Под график' ? qty(signal.raw_shortage_qty) : '—'}</td>
                        <td className="numCell"><strong>{qty(signal.suggested_qty)}</strong></td>
                        <td className="numCell">{signal.status === 'Diagnostic' ? qty(signal.calculated_batch_qty) : '—'}</td>
                        <td>{signal.drum_slot_id ? `№${signal.drum_slot_id}` : '—'}</td>
                        <td>{signal.is_incomplete ? <span className="dbrQualityWarning" title={(signal.data_quality ?? []).map((reason) => REASON_LABEL[reason] ?? reason).join(', ')}>⚠ Неполные данные</span> : <span className="dbrQualityOk">Полные</span>}</td>
                        <td>{signal.status === 'Open' ? 'Открыт' : signal.status === 'Diagnostic' ? 'Диагностика' : signal.status === 'Cancelled' ? 'Отменён' : signal.status}</td>
                        <td>{dateTimeRu(signal.refreshed_at) || '—'}</td>
                        <td className="dbrActionCell" onClick={(e) => e.stopPropagation()}>
                          {signal.can_launch && signal.status === 'Open' && (
                            <button className="dbrLaunchBtn" onClick={() => void startLaunch(signal)} disabled={launchBusy} title="Запустить в производство (создать заказ в 1С)">Запустить…</button>
                          )}
                        </td>
                      </tr>
                      {isExpanded && hasDeficit && (
                        <tr className="dbrDeficitExpand">
                          <td />
                          <td />
                          <td colSpan={18}>
                            <div className="dbrDeficitLines">
                              <div className="dbrDeficitLinesTitle">Дефицит комплекта</div>
                              <table className="dbrDeficitLinesTable">
                                <thead><tr><th>Позиция</th><th className="numCell">Нужно</th><th className="numCell">Есть</th><th className="numCell">Не хватает</th><th>Тип</th></tr></thead>
                                <tbody>
                                  {deficitLines.map((line) => (
                                    <tr key={line.item}>
                                      <td><strong>{line.item}</strong><span className="dbrFeederItemName">{line.item_name}</span></td>
                                      <td className="numCell">{qty(line.need)}</td>
                                      <td className="numCell">{qty(line.have)}</td>
                                      <td className="numCell"><strong>{qty(Math.max(line.need - line.have, 0))}</strong></td>
                                      <td>{SOURCE_LABEL[line.kind] ?? line.kind}{line.level ? ` · ${line.level}` : ''}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                              {(signal.root_items?.length ?? 0) > 0 && (
                                <div className="dbrRootItems">Изделия-потребители: {signal.root_items!.map((r) => r.item).join(', ')}</div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
            {selectedSignal && (
              <aside className="dbrSignalDetail" aria-label="Карточка сигнала">
                <div className="dbrSignalDetailTitle"><strong>Сигнал #{selectedSignal.id}</strong><button aria-label="Закрыть карточку" onClick={() => setSelectedSignal(null)}>×</button></div>
                <dl>
                  <dt>Номенклатура</dt><dd>{selectedSignal.item_code}<small>{selectedSignal.item_name}</small></dd>
                  <dt>Склад</dt><dd>{selectedSignal.warehouse_ref1c}</dd>
                  <dt>Тип</dt><dd><span className={`dbrSignalTypeBadge ${selectedSignal.signal_type === 'Под график' ? 'schedule' : 'replenish'}`}>{SIGNAL_TYPE_LABEL[selectedSignal.signal_type] ?? selectedSignal.signal_type}</span></dd>
                  <dt>Предложено</dt><dd>{qty(selectedSignal.suggested_qty)}</dd>
                  {selectedSignal.status === 'Diagnostic' && <><dt>Расчётная партия</dt><dd>{qty(selectedSignal.calculated_batch_qty)} <small>не рекомендация</small></dd></>}
                  {selectedSignal.signal_type === 'Под график' && <>
                    <dt>Крайний срок запуска</dt><dd>{dateRu(selectedSignal.need_date) || '—'}</dd>
                    <dt>Дата потребности / слота</dt><dd>{dateRu(selectedSignal.required_date) || '—'}</dd>
                    <dt>Спрос / дефицит</dt><dd>{qty(selectedSignal.raw_demand_qty)} / {qty(selectedSignal.raw_shortage_qty)}</dd>
                    <dt>Барабанный слот</dt><dd>№{selectedSignal.drum_slot_id ?? '—'}</dd>
                  </>}
                  <dt>NFP / цель</dt><dd>{qty(selectedSignal.nfp_snapshot)} / {qty(selectedSignal.target_qty_snapshot)}</dd>
                  <dt>KIT-дефицит</dt><dd>{selectedSignal.kit_force ? qty(selectedSignal.kit_shortage_qty) : 'нет'}</dd>
                  <dt>График</dt><dd>№{selectedSignal.source_schedule_id ?? 'нет'}</dd>
                  <dt>Источник</dt><dd>{selectedSignal.reason_json?.generator ?? '—'}</dd>
                  <dt>Качество</dt><dd>{selectedSignal.is_incomplete || selectedSignal.data_quality?.length || selectedSignal.reason_json?.missing_reasons?.length ? <span className="dbrQualityWarning">⚠ {[...(selectedSignal.data_quality ?? []), ...(selectedSignal.reason_json?.missing_reasons ?? [])].filter((reason, index, all) => all.indexOf(reason) === index).map((reason) => REASON_LABEL[reason] ?? reason).join(', ') || 'Неполные данные'}</span> : 'Полные данные'}</dd>
                  {selectedSignal.material_status && <><dt>Материал</dt><dd>{selectedSignal.material_status}</dd></>}
                </dl>
                {selectedSignal.status === 'Open' && selectedSignal.can_launch ? (
                  <div className="dbrSignalActions">
                    <button className="dbrDanger" onClick={() => void startLaunch(selectedSignal)} disabled={launchBusy}>Запустить в производство…</button>
                    <div className="fieldHint">Создаст заказ на производство в живой 1С. Сначала откроется предпросмотр документа.</div>
                  </div>
                ) : (
                  <div className="dbrSignalReadonly">
                    {selectedSignal.status !== 'Open'
                      ? 'Запуск доступен только для открытых сигналов.'
                      : 'Запуск заблокирован: материальная готовность не подтверждена (см. дефицит комплекта).'}
                  </div>
                )}
              </aside>
            )}
          </div>
        </section>

        <section className="dbrSignalSection dbrDeficitSection" aria-label="Материальные дефициты очереди">
          <div className="dbrSignalHeader">
            <div>
              <h2>Дефициты очереди</h2>
              <p>Блокирующие позиции комплектов открытой очереди: чего и на сколько не хватает, сколько сигналов держит нехватка. Клик по позиции оставляет в очереди только заблокированные ею сигналы.</p>
            </div>
            <div className="dbrSignalHeaderActions">
              {deficits && <span className="dbrSignalCount">Дефицитных позиций: {deficits.kpis.deficit_materials}; открытых сигналов: {deficits.kpis.queue_open}</span>}
              <button onClick={() => void loadDeficits()} disabled={deficitsLoading}>Обновить</button>
            </div>
          </div>

          <div className="dbrFeederTableWrap dbrDeficitTableWrap">
            <table className="journalTable dbrTable dbrDeficitTable">
              <thead><tr>
                <th className={`dbrSortable ${deficitSort === 'item' ? 'active' : ''}`} onClick={() => setDeficitSort('item')}>Позиция</th>
                <th>Тип</th>
                <th className={`numCell dbrSortable ${deficitSort === 'short_qty' ? 'active' : ''}`} onClick={() => setDeficitSort('short_qty')}>Не хватает</th>
                <th className="numCell">Потребность</th>
                <th className="numCell">Есть</th>
                <th className={`numCell dbrSortable ${deficitSort === 'blocks_signals' ? 'active' : ''}`} onClick={() => setDeficitSort('blocks_signals')}>Держит сигналов</th>
                <th className={`dbrSortable ${deficitSort === 'nearest_due' ? 'active' : ''}`} onClick={() => setDeficitSort('nearest_due')}>Ближайший срок</th>
              </tr></thead>
              <tbody>
                {!deficitsLoading && !sortedDeficits.length && <tr><td colSpan={7} className="emptyCell">Дефицитов в открытой очереди нет.</td></tr>}
                {sortedDeficits.map((deficit) => (
                  <tr key={deficit.item} className={`dbrDeficitRow ${deficitFilter === deficit.item ? 'selected' : ''}`} onClick={() => filterByDeficit(deficit)} title="Отфильтровать очередь по этой позиции">
                    <td><strong>{deficit.item}</strong><span className="dbrFeederItemName">{deficit.item_name}</span></td>
                    <td><span className={`dbrSourceBadge ${deficit.source}`}>{SOURCE_LABEL[deficit.source] ?? deficit.source}</span></td>
                    <td className="numCell"><strong>{qty(deficit.short_qty)}</strong></td>
                    <td className="numCell">{qty(deficit.need_sum)}</td>
                    <td className="numCell">{qty(deficit.gross)}</td>
                    <td className="numCell">{deficit.blocks_signals}</td>
                    <td>{dateRu(deficit.nearest_due) || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="dbrSignalReadonly">Только просмотр: запас считается «выбранные − игнорируемые» склады ({deficits?.kpis.stock_source ?? 'selected − ignored'}), без запуска производства и заказов.</div>
        </section>

        <section className="dbrSignalSection dbrProcessingSection" aria-label="Давальческий контур переработки">
          <div className="dbrSignalHeader">
            <div>
              <h2>Переработка (давальческий контур)</h2>
              <p>Полка покрытой детали: NFP = остаток + труба переработчика + голая (остаток и в работе). Возраст партии считается от даты заказа переработчику — дата фактической отправки в 1С не синхронизируется.</p>
            </div>
            <div className="dbrSignalHeaderActions">
              {processingBoard && (
                <span className={`dbrSignalCount ${processingBoard.overdue_positions ? 'dbrOverdueBadge' : ''}`}>
                  Позиций: {processingBoard.positions_total}; просрочен кругорейс (&gt;{processingBoard.roundtrip_limit_days} дн): {processingBoard.overdue_positions}
                </span>
              )}
              <button onClick={() => void loadProcessingBoard()} disabled={processingLoading}>Обновить</button>
            </div>
          </div>

          <div className="dbrFeederTableWrap">
            <table className="journalTable dbrTable">
              <thead><tr>
                <th>Позиция</th><th>Зона</th>
                <th className="numCell">NFP</th><th className="numCell">Полка</th>
                <th className="numCell">У переработчика</th><th className="numCell">Голая (остаток+WIP)</th>
                <th className="numCell">Target</th><th className="numCell">ADU</th>
                <th>Открытые заказы переработчику</th>
              </tr></thead>
              <tbody>
                {!processingLoading && !(processingBoard?.positions.length) && (
                  <tr><td colSpan={9} className="emptyCell">Processing-позиций нет — пересчитайте позиции по активному графику.</td></tr>
                )}
                {(processingBoard?.positions ?? []).map((row) => (
                  <tr key={row.position_id} className={row.has_overdue ? 'dbrProcessingOverdueRow' : ''}>
                    <td><strong>{row.item_article || row.item_code}</strong><span className="dbrFeederItemName">{row.item_name}</span></td>
                    <td>{row.zone ? <span className={`dbrZoneBadge ${String(row.zone).toLowerCase()}`}>{ZONE_LABEL[String(row.zone).toLowerCase()] ?? row.zone}</span> : '—'}</td>
                    <td className="numCell"><strong>{row.nfp != null ? qty(row.nfp) : '—'}</strong></td>
                    <td className="numCell">{row.stock_qty != null ? qty(row.stock_qty) : '—'}</td>
                    <td className="numCell">{row.open_supply_qty != null ? qty(row.open_supply_qty) : '—'}</td>
                    <td className="numCell">{row.chain_supply_qty != null ? qty(row.chain_supply_qty) : '—'}</td>
                    <td className="numCell">{qty(row.target_qty)}</td>
                    <td className="numCell">{qty(row.adu)}</td>
                    <td>
                      {row.open_orders.length
                        ? row.open_orders.map((order) => (
                          <div key={`${order.order_id}:${order.line_id}`} className={order.overdue ? 'dbrOverdueOrder' : ''} title={order.overdue ? `Партия у подрядчика дольше ${processingBoard?.roundtrip_limit_days} дн` : undefined}>
                            {order.order_number} · {qty(order.remaining_qty)} шт · {order.age_days != null ? `${order.age_days} дн` : '—'}{order.overdue ? ' ⚠' : ''}
                          </div>
                        ))
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="dbrSignalReadonly">Только просмотр: сигналы переработки advisory, заказ переработчику создаётся вручную (материализация — после смоука вида операции в 1С).</div>
        </section>

        {chainPreview && (
          <div className="dialogOverlay" role="dialog" aria-modal="true" aria-label="Предпросмотр цепочки" onClick={() => setChainPreview(null)}>
            <div className="dialogBox" onClick={(e) => e.stopPropagation()}>
              <div className="dialogHeader">Цепочка: предпросмотр первого уровня</div>
              <div className="dialogBody">
                <div className="dbrChainSummary">
                  <div><span className="dbrChainSummaryLabel">Состояние</span><strong>{chainPreview.enabled ? 'включена' : 'выключена'}</strong></div>
                  <div><span className="dbrChainSummaryLabel">Открытых сигналов</span><strong>{chainPreview.open_signals}</strong></div>
                  <div><span className="dbrChainSummaryLabel">Дочерних (1-й уровень)</span><strong>{chainPreview.level1_children}</strong></div>
                  <div><span className="dbrChainSummaryLabel">Уникальных заготовок</span><strong>{chainPreview.distinct_items}</strong></div>
                </div>
                {chainPreview.top_items.length > 0 ? (
                  <table className="dbrDeficitLinesTable">
                    <thead><tr><th>Заготовка</th><th className="numCell">Родителей</th><th className="numCell">Сумма кол-ва</th></tr></thead>
                    <tbody>
                      {chainPreview.top_items.map((item) => (
                        <tr key={item.item}><td><strong>{item.item}</strong></td><td className="numCell">{item.parents}</td><td className="numCell">{qty(item.qty_sum)}</td></tr>
                      ))}
                    </tbody>
                  </table>
                ) : <div className="dbrChainEmpty">Дочерних заготовок первого уровня нет.</div>}
                <div className="dbrSignalReadonly">Предпросмотр не создаёт сигналы. Глубокие уровни видны только после «Цепочка: обновить».</div>
              </div>
              <div className="dialogFooter">
                <button onClick={() => setChainPreview(null)}>Закрыть</button>
                {chainEnabled && <button className="primary" onClick={() => void runChainRefresh()} disabled={saving}>Цепочка: обновить</button>}
              </div>
            </div>
          </div>
        )}

        {/* ── Launch one signal into a 1С production order ──────────────── */}
        {launchFlow && (
          <DbrConfirmDialog
            title={`Запуск сигнала — ${launchFlow.signal.item_code ?? `#${launchFlow.signal.id}`}`}
            phase={launchFlow.result ? 'done' : launchFlow.deficit ? 'blocked' : 'preview'}
            busy={launchBusy}
            confirmLabel="Провести в 1С"
            error={launchError}
            onClose={closeLaunch}
            onConfirm={() => void confirmLaunch()}
          >
            {(() => {
              const { signal, preview, result, deficit } = launchFlow
              if (deficit) {
                return (
                  <div className="dbrDeficitLines">
                    <div className="dbrDeficitLinesTitle">Запуск заблокирован материальным дефицитом</div>
                    {deficit.length ? (
                      <table className="dbrDeficitLinesTable">
                        <thead><tr><th>Позиция</th><th className="numCell">Нужно</th><th className="numCell">Есть</th><th className="numCell">Не хватает</th><th>Тип</th></tr></thead>
                        <tbody>
                          {deficit.map((line) => (
                            <tr key={line.item}>
                              <td><strong>{line.item}</strong><span className="dbrFeederItemName">{line.item_name}</span></td>
                              <td className="numCell">{qty(line.need)}</td>
                              <td className="numCell">{qty(line.have)}</td>
                              <td className="numCell"><strong>{qty(Math.max(line.need - line.have, 0))}</strong></td>
                              <td>{SOURCE_LABEL[line.kind] ?? line.kind}{line.level ? ` · ${line.level}` : ''}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : <div className="fieldHint">Детализация дефицита недоступна.</div>}
                  </div>
                )
              }
              const doc = result ?? preview
              return (
                <>
                  <div className="dbrDocSummary">
                    <div><span>Документ</span><strong>Заказ на производство{doc ? ` · № ${doc.number}` : ''}</strong></div>
                    <div><span>Изделие</span><strong>{signal.item_code} — {signal.item_name}</strong></div>
                    <div><span>Количество</span><strong>{qty(signal.suggested_qty)} шт</strong></div>
                    <div><span>Склад-приёмник</span><strong>{signal.warehouse_ref1c}</strong></div>
                    {signal.required_date && <div><span>Дата потребности</span><strong>{dateRu(signal.required_date)}</strong></div>}
                  </div>
                  {result && (
                    <div className="dbrResultBox">
                      {result.created ? (
                        <p>Заказ создан в 1С: <strong>№ {result.number}</strong>{result.one_c_order_ref ? <span className="dbrRefKey"> · ref {result.one_c_order_ref}</span> : null}</p>
                      ) : result.already_launched ? (
                        <p>Заказ уже был создан ранее: <strong>№ {result.number}</strong>{result.one_c_order_ref ? <span className="dbrRefKey"> · ref {result.one_c_order_ref}</span> : null}</p>
                      ) : result.error ? (
                        <p className="dbrResultError">Ошибка записи в 1С: {result.error}</p>
                      ) : (
                        <p>{result.note}</p>
                      )}
                    </div>
                  )}
                  {!preview && !result && <div className="fieldHint">Загрузка предпросмотра…</div>}
                  {preview?.payload && (
                    <details className="dbrPayloadDetails">
                      <summary>Показать payload документа 1С</summary>
                      <pre className="dialogPreview">{JSON.stringify(preview.payload, null, 2)}</pre>
                    </details>
                  )}
                </>
              )
            })()}
          </DbrConfirmDialog>
        )}

        {/* ── Mass supplier order for «Пополнение» signals ──────────────── */}
        {purchaseFlow && (
          <DbrConfirmDialog
            title="Заказ поставщику по сигналам питателя"
            phase={purchaseFlow.result ? 'done' : 'preview'}
            busy={purchaseBusy}
            confirmLabel="Провести в 1С"
            error={purchaseError}
            onClose={() => setPurchaseFlow(null)}
            onConfirm={() => void confirmPurchase()}
          >
            {(() => {
              const data = purchaseFlow.result ?? purchaseFlow.preview
              if (!data) return <div className="fieldHint">Загрузка предпросмотра…</div>
              return (
                <>
                  <div className="fieldHint">
                    {purchaseFlow.signalIds?.length
                      ? `Выбрано сигналов: ${purchaseFlow.signalIds.length}.`
                      : 'Выбраны все открытые закупочные сигналы «Пополнение».'}
                  </div>
                  <DbrPurchaseResultBody data={data} />
                </>
              )
            })()}
          </DbrConfirmDialog>
        )}

        <div className="dbrKpis dbrFeederKpis">
          <div className="dbrKpi"><div className="dbrKpiLabel">Позиции</div><div className="dbrKpiValue">{rows.length}</div><div className="dbrKpiSub">активные, по фильтру</div></div>
          <div className="dbrKpi"><div className="dbrKpiLabel">Зоны NFP</div><div className="dbrKpiValue"><span className="dbrDot g" />{summary.green ?? 0}<span className="dbrDot y" />{summary.yellow ?? 0}<span className="dbrDot r" />{summary.red ?? 0}</div><div className="dbrKpiSub">состояние на момент обновления</div></div>
          <div className={`dbrKpi ${summary.incomplete ? 'alert' : ''}`}><div className="dbrKpiLabel">Неполные данные</div><div className="dbrKpiValue">{summary.incomplete ?? 0}</div><div className="dbrKpiSub">не включать в автоматические решения</div></div>
        </div>

        <div className="dbrFeederTableWrap">
          <table className="journalTable dbrTable dbrFeederTable">
            <thead><tr><th>Зона</th><th>Номенклатура</th><th>Склад</th><th>Контур</th><th>Режим</th><th className="numCell">Остаток</th><th className="numCell">Приход</th><th className="numCell">Спрос</th><th className="numCell">NFP</th><th className="numCell">Цель</th><th>Качество</th></tr></thead>
            <tbody>
              {!loading && !rows.length && <tr><td colSpan={11} className="emptyCell">Позиции не найдены</td></tr>}
              {rows.map((row) => {
                const live = row.live_nfp
                const normalizedZone = zoneKey(live?.zone)
                const reasons = live?.missing_reasons.map((reason) => REASON_LABEL[reason] ?? reason) ?? []
                const quality = [...reasons, ...(live?.data_quality ?? row.data_quality ?? [])]
                return (
                  <tr key={row.id} className={!live?.is_complete ? 'dbrFeederIncomplete' : undefined}>
                    <td><span className={`dbrZoneBadge ${normalizedZone}`}><span className={`dbrDot ${normalizedZone === 'unknown' ? 'n' : normalizedZone.slice(0, 1)}`} />{ZONE_LABEL[normalizedZone] ?? 'Нет данных'}</span></td>
                    <td><strong>{row.item_code}</strong><span className="dbrFeederItemName">{row.item_name}</span></td>
                    <td title={row.warehouse_ref1c}>{row.warehouse_ref1c}</td>
                    <td>{SUPPLY_LABEL[row.supply_type] ?? row.supply_type}</td>
                    <td>{MODE_LABEL[row.mode] ?? row.mode}</td>
                    <td className="numCell">{qty(live?.stock_qty)}</td>
                    <td className="numCell">{qty(live?.open_supply_qty)}</td>
                    <td className="numCell">{qty(live?.qualified_demand_qty)}</td>
                    <td className="numCell"><strong>{qty(live?.nfp)}</strong></td>
                    <td className="numCell">{qty(row.target_qty)}</td>
                    <td>{quality.length ? <details className="dbrQuality"><summary>⚠ {quality.length}</summary><ul>{quality.map((note, index) => <li key={`${note}-${index}`}>{note}</li>)}</ul><small>Остатки: {dateTimeRu(live?.timestamps.stock_as_of) || 'нет даты'}<br />Приходы: {dateTimeRu(live?.timestamps.supply_as_of) || 'нет даты'}</small></details> : <span className="dbrQualityOk">Полные</span>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
