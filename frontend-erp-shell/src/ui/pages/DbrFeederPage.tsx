import { useCallback, useEffect, useMemo, useState } from 'react'
import type { DbrFeederPosition, DbrFeederPreview, DbrFeederSignal, DbrFeederSignalPreview } from '../../domain/dbr'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import {
  listDbrFeederPositions,
  getDbrFeederSignal,
  listDbrFeederSignals,
  previewDbrFeederPositions,
  previewDbrFeederSignals,
  rebuildDbrFeederPositions,
  refreshDbrFeederSignals,
} from '../../services/dbr'
import { DbrNav } from '../dbr/DbrNav'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const ZONE_LABEL: Record<string, string> = { green: 'Зелёная', yellow: 'Жёлтая', red: 'Красная' }
const MODE_LABEL: Record<string, string> = { shelf: 'Полка', under_schedule: 'Под график' }
const SUPPLY_LABEL: Record<string, string> = { purchase: 'Закупка', manufacture: 'Производство' }
const REASON_LABEL: Record<string, string> = {
  open_supply_destination_missing: 'не указан склад открытого прихода',
  stale_schedule: 'позиция рассчитана не по активному графику',
  production_inbound_destination_missing: 'не указан склад производственного прихода',
  production_inbound_eta_missing: 'не указана дата производственного прихода',
  supplier_inbound_destination_missing: 'не указан склад прихода поставщика',
  supplier_inbound_eta_missing: 'не указана дата прихода поставщика',
}
const SIGNAL_TYPE_LABEL: Record<string, string> = { 'Пополнение': 'Пополнение', 'Под график': 'Под график' }

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
      await loadSignals(appliedSignalFilters)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
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
            <option value="">Все типы</option><option value="purchase">Закупка</option><option value="manufacture">Производство</option>
          </select>
          <button onClick={applyFilters} disabled={loading}>Применить</button>
          <button onClick={resetFilters} disabled={loading}>Сбросить</button>
          <div className="commandBarSpacer" />
          <button onClick={() => void calculatePreview()} disabled={saving}>Предпросмотр пересчёта</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}
        <div className="dbrFeederNotice">Экран не создаёт заказы, не запускает производство и не отправляет данные в 1С.</div>

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
            <button onClick={() => void calculateSignalPreview()} disabled={saving}>Предпросмотр сигналов</button>
          </div>

          {signalPreview && (
            <div className="dbrFeederPreview dbrSignalPreview" aria-label="Предпросмотр обновления сигналов">
              <div>
                <strong>График №{signalPreview.schedule_id ?? 'не активен'}: {signalPreview.actionable} актуальных сигналов</strong>
                <span>Пополнение: {signalPreviewSummary.replenish}; под график: {signalPreviewSummary.underSchedule}</span>
                <span>Позиций проверено: {signalPreview.positions}; открыть: {signalPreview.rows.filter((row) => row.action === 'open').length}, обновить: {signalPreview.rows.filter((row) => row.action === 'update').length}, отменить: {signalPreview.rows.filter((row) => row.action === 'cancel').length}</span>
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
              <option value="Open">Открытые</option><option value="Cancelled">Отменённые</option><option value="">Все статусы</option>
            </select>
            <select aria-label="Зона сигнала" value={signalFilters.zone} onChange={(e) => setSignalFilters({ ...signalFilters, zone: e.target.value })}>
              <option value="">Все зоны</option><option value="red">Красная</option><option value="yellow">Жёлтая</option><option value="green">Зелёная</option>
            </select>
            <select aria-label="Тип сигнала" value={signalFilters.signal_type} onChange={(e) => setSignalFilters({ ...signalFilters, signal_type: e.target.value })}>
              <option value="">Все типы</option><option value="Пополнение">Пополнение</option><option value="Под график">Под график</option>
            </select>
            <button onClick={() => setAppliedSignalFilters(signalFilters)} disabled={signalsLoading}>Применить</button>
            <button onClick={() => { setSignalFilters(EMPTY_SIGNAL_FILTERS); setAppliedSignalFilters(EMPTY_SIGNAL_FILTERS) }} disabled={signalsLoading}>Сбросить</button>
            <div className="commandBarSpacer" />
            <span className="dbrSignalCount">Сигналов: {signals.length}</span>
          </div>

          <div className="dbrSignalLayout">
            <div className="dbrFeederTableWrap">
              <table className="journalTable dbrTable dbrSignalTable">
                <thead><tr><th>Тип</th><th>KIT</th><th>Приоритет</th><th>Зона</th><th>Номенклатура</th><th>Склад</th><th>Крайний срок запуска</th><th>Дата потребности / слота</th><th className="numCell">Спрос</th><th className="numCell">Дефицит</th><th className="numCell">Количество</th><th>Слот</th><th>Качество</th><th>Статус</th><th>Обновлён</th></tr></thead>
                <tbody>
                  {!signalsLoading && !signals.length && <tr><td colSpan={15} className="emptyCell">Сигналы не найдены. Выполните предпросмотр и явное обновление.</td></tr>}
                  {signals.map((signal) => {
                    const normalizedZone = zoneKey(signal.zone)
                    return (
                      <tr key={signal.id} className={`${selectedSignal?.id === signal.id ? 'selected' : ''} ${signal.kit_force ? 'dbrSignalKitRow' : ''} ${signal.is_incomplete ? 'dbrFeederIncomplete' : ''}`} onClick={() => void selectSignal(signal.id)}>
                        <td><span className={`dbrSignalTypeBadge ${signal.signal_type === 'Под график' ? 'schedule' : 'replenish'}`}>{SIGNAL_TYPE_LABEL[signal.signal_type] ?? signal.signal_type}</span></td>
                        <td>{signal.kit_force ? <span className="dbrKitForce">KIT</span> : '—'}</td>
                        <td className="numCell"><strong>{Number(signal.priority).toFixed(2)}</strong></td>
                        <td><span className={`dbrZoneBadge ${normalizedZone}`}><span className={`dbrDot ${normalizedZone.slice(0, 1)}`} />{ZONE_LABEL[normalizedZone] ?? signal.zone}</span></td>
                        <td><strong>{signal.item_code ?? `#${signal.item_id}`}</strong><span className="dbrFeederItemName">{signal.item_name}</span></td>
                        <td title={signal.warehouse_ref1c}>{signal.warehouse_ref1c}</td>
                        <td>{signal.signal_type === 'Под график' ? dateRu(signal.need_date) || '—' : '—'}</td>
                        <td>{signal.signal_type === 'Под график' ? dateRu(signal.required_date) || '—' : '—'}</td>
                        <td className="numCell">{signal.signal_type === 'Под график' ? qty(signal.raw_demand_qty) : '—'}</td>
                        <td className="numCell">{signal.signal_type === 'Под график' ? qty(signal.raw_shortage_qty) : '—'}</td>
                        <td className="numCell"><strong>{qty(signal.suggested_qty)}</strong></td>
                        <td>{signal.drum_slot_id ? `№${signal.drum_slot_id}` : '—'}</td>
                        <td>{signal.is_incomplete ? <span className="dbrQualityWarning" title={(signal.data_quality ?? []).map((reason) => REASON_LABEL[reason] ?? reason).join(', ')}>⚠ Неполные данные</span> : <span className="dbrQualityOk">Полные</span>}</td>
                        <td>{signal.status === 'Open' ? 'Открыт' : signal.status === 'Cancelled' ? 'Отменён' : signal.status}</td>
                        <td>{dateTimeRu(signal.refreshed_at) || '—'}</td>
                      </tr>
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
                </dl>
                <div className="dbrSignalReadonly">Только просмотр: исполнительные действия отсутствуют.</div>
              </aside>
            )}
          </div>
        </section>

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
