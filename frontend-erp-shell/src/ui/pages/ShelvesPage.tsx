import { Fragment, useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { dateRu, qty } from '../../lib/format'
import { unavailableTruth } from '../../lib/api'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { TruthUnavailableNotice } from '../TruthUnavailableNotice'
import { listShelves } from '../../services/shelves'
import type { ShelfDemandManifestEntry, ShelfProjectionRow } from '../../domain/shelves'

const PAGE_LIMIT = 100
const COLUMN_COUNT = 16

function formatNumber(value: number) {
  return qty(value)
}

function formatDate(value: string | null) {
  if (!value) return '—'
  return dateRu(value)
}

function formatPriority(key: Array<string | number>) {
  return key.join(' · ')
}

function itemTitle(row: ShelfProjectionRow) {
  return row.item_name ?? `#${row.item_id}`
}

function DemandManifest({ entries }: { entries: ShelfDemandManifestEntry[] }) {
  if (!entries.length) {
    return <div className="emptyDetail">Полка ничего не защищает в текущем барабане</div>
  }
  return (
    <table className="journalTable">
      <thead>
        <tr>
          <th>Нужно к дате</th>
          <th className="numCell">Количество</th>
          <th className="numCell">План</th>
          <th className="numCell">Строка плана</th>
          <th className="numCell">Слот барабана</th>
          <th>Приоритет</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr key={entry.drum_slot_id + ':' + entry.freeze_component_id}>
            <td>{formatDate(entry.need_date)}</td>
            <td className="numCell">{qty(entry.qty)}</td>
            <td className="numCell">{entry.plan_id}</td>
            <td className="numCell">{entry.plan_line_id}</td>
            <td className="numCell">{entry.drum_slot_id}</td>
            <td>{formatPriority(entry.priority)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function ShelvesPage() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<ShelfProjectionRow[]>([])
  const [totalRows, setTotalRows] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [unavailable, setUnavailable] = useState('')
  const [expandedPolicyId, setExpandedPolicyId] = useState<number | null>(null)

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setUnavailable('')
    try {
      const data = await listShelves({ limit: PAGE_LIMIT, offset: nextOffset })
      setRows(data.rows ?? [])
      setTotalRows(Number(data.total_rows || 0))
      setOffset(Number(data.offset ?? nextOffset))
      setExpandedPolicyId(null)
    } catch (e) {
      const blocked = unavailableTruth(e)
      if (blocked) setUnavailable(blocked.reason)
      else setError(e instanceof Error ? e.message : String(e))
      setRows([])
      setTotalRows(0)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(0)
  }, [load])

  const visibleFrom = rows.length ? offset + 1 : 0
  const visibleTo = offset + rows.length

  return (
    <main className="workArea">
      <DocumentWindow
        title="Полки"
        subtitle="Срез рассчитанной проекции по полкам из подтверждённого планового состояния"
        hotkeys="На клиенте нет пересчёта — только отображение persisted read-model"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={totalRows}
            selectedCount={0}
            canPrev={offset > 0}
            canNext={visibleTo < totalRows}
            onPrev={() => void load(Math.max(0, offset - PAGE_LIMIT))}
            onNext={() => void load(offset + PAGE_LIMIT)}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => void load(offset)} disabled={loading}>Обновить</button>
          <button onClick={() => navigate('/')} disabled={loading}>На главную</button>
          <div className="toolbarText">Строк: {totalRows}</div>
          {totalRows > PAGE_LIMIT && (
            <div className="toolbarText">Показаны {visibleFrom}–{visibleTo}</div>
          )}
        </div>

        {unavailable && <TruthUnavailableNotice reason={unavailable} />}
        {error && <div className="errorLine">{error}</div>}

        <div style={{ overflow: 'auto' }}>
          <table className="journalTable">
            <thead>
              <tr>
                <th className="numCell">Полка</th>
                <th>Код</th>
                <th>Номенклатура</th>
                <th>Склад</th>
                <th className="numCell">Защищено до</th>
                <th className="numCell">Цель</th>
                <th className="numCell">Наличие на полке</th>
                <th className="numCell">Другой склад</th>
                <th className="numCell">Перенос</th>
                <th className="numCell">Дефицит</th>
                <th className="numCell">Unlaunched</th>
                <th className="numCell">Pull</th>
                <th className="numCell">Материализовано</th>
                <th>Первый дефицит</th>
                <th>Последний старт</th>
                <th className="numCell">Защищает</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={COLUMN_COUNT}>Загрузка...</td>
                </tr>
              ) : null}
              {!loading && !rows.length && !unavailable && !error ? (
                <tr>
                  <td colSpan={COLUMN_COUNT}>Нет строк для текущей правды</td>
                </tr>
              ) : null}
              {!loading && rows.map((row) => {
                const expanded = expandedPolicyId === row.policy_id
                return (
                  <Fragment key={`${row.policy_id}-${row.item_id}-${row.warehouse_ref1c}`}>
                    <tr
                      className={expanded ? 'activeRow' : ''}
                      style={{ cursor: row.demand_manifest.length ? 'pointer' : undefined }}
                      onClick={() => setExpandedPolicyId(expanded ? null : row.policy_id)}
                    >
                      <td className="numCell">{row.policy_id}</td>
                      <td><span className="muted">{row.item_code ?? `#${row.item_id}`}</span></td>
                      <td><strong>{itemTitle(row)}</strong></td>
                      <td>{row.warehouse_ref1c}</td>
                      <td className="numCell">{formatDate(row.protection_until)}</td>
                      <td className="numCell">{formatNumber(row.target_qty)}</td>
                      <td className="numCell">{formatNumber(row.shelf_physical_qty)}</td>
                      <td className="numCell">{formatNumber(row.other_stock_qty)}</td>
                      <td className="numCell">{formatNumber(row.transfer_qty)}</td>
                      <td className="numCell">{formatNumber(row.gap_qty)}</td>
                      <td className="numCell">{formatNumber(row.unlaunched_mrp_qty)}</td>
                      <td className="numCell">{formatNumber(row.pull_qty)}</td>
                      <td className="numCell">{formatNumber(row.materialized_qty)}</td>
                      <td>{formatDate(row.first_shortage_date)}</td>
                      <td>{formatDate(row.latest_start_date)}</td>
                      <td className="numCell">{row.demand_manifest.length}</td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td colSpan={COLUMN_COUNT}>
                          <div style={{ padding: '6px 0' }}>
                            <div className="toolbarText" style={{ marginBottom: 4 }}>
                              Что защищает полка «{itemTitle(row)}» на складе {row.warehouse_ref1c}
                            </div>
                            <DemandManifest entries={row.demand_manifest} />
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
      </DocumentWindow>
    </main>
  )
}
