import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { dateRu, qty } from '../../lib/format'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { listShelves } from '../../services/shelves'
import type { ShelfProjectionRow } from '../../domain/shelves'

function formatNumber(value: number) {
  return qty(value)
}

function formatDate(value: string | null) {
  if (!value) return '—'
  return dateRu(value)
}

export function ShelvesPage() {
  const navigate = useNavigate()
  const [rows, setRows] = useState<ShelfProjectionRow[]>([])
  const [totalRows, setTotalRows] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await listShelves()
      setRows(data.rows ?? [])
      setTotalRows(Number(data.total_rows || 0))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <main className="workArea">
      <DocumentWindow
        title="Полки"
        subtitle="Срез рассчитанной проекции по полкам из подтверждённого планового состояния"
        hotkeys="На клиенте нет пересчёта — только отображение persisted read-model"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={totalRows ? 1 : 0}
            visibleTo={totalRows}
            total={totalRows}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
          <button onClick={() => navigate('/')} disabled={loading}>На главную</button>
          <div className="toolbarText">Строк: {totalRows}</div>
        </div>

        {error && <div className="errorLine">{error}</div>}

        <div style={{ overflow: 'auto' }}>
          <table className="journalTable">
            <thead>
              <tr>
                <th className="numCell">Полка</th>
                <th className="numCell">Товар</th>
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
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={14}>Загрузка...</td>
                </tr>
              ) : null}
              {!loading && !rows.length ? (
                <tr>
                  <td colSpan={14}>Нет строк для текущей правды</td>
                </tr>
              ) : null}
              {!loading && rows.map((row) => (
                <tr key={`${row.policy_id}-${row.item_id}-${row.warehouse_ref1c}`}>
                  <td className="numCell">{row.policy_id}</td>
                  <td className="numCell">{row.item_id}</td>
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
