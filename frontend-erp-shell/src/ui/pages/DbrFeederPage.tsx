import { useCallback, useEffect, useMemo, useState } from 'react'
import type { DbrFeederPosition, DbrFeederPreview } from '../../domain/dbr'
import { dateTimeRu, qty } from '../../lib/format'
import {
  listDbrFeederPositions,
  previewDbrFeederPositions,
  rebuildDbrFeederPositions,
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
}

type Filters = { search: string; zone: string; mode: string; supply: string }
const EMPTY_FILTERS: Filters = { search: '', zone: '', mode: '', supply: '' }

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

  const summary = useMemo(() => rows.reduce((acc, row) => {
    const zone = zoneKey(row.live_nfp?.zone)
    acc[zone] = (acc[zone] ?? 0) + 1
    if (!row.live_nfp?.is_complete) acc.incomplete = (acc.incomplete ?? 0) + 1
    return acc
  }, {} as Record<string, number>), [rows])

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
