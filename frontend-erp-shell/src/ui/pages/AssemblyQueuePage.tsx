import { useCallback, useEffect, useState } from 'react'
import { dateRu, qty } from '../../lib/format'
import { unavailableTruth } from '../../lib/api'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { TruthUnavailableNotice } from '../TruthUnavailableNotice'
import { listAssemblyQueue } from '../../services/assemblyQueue'
import type { AssemblyQueueRow } from '../../domain/assemblyQueue'

const PAGE_LIMIT = 100

function formatPriority(key: Array<string | number>) {
  return key.join(' · ')
}

function formatPeriod(value: string) {
  return dateRu(value)
}

function formatNumber(value: number) {
  return qty(value)
}

export function AssemblyQueuePage() {
  const [rows, setRows] = useState<AssemblyQueueRow[]>([])
  const [totalRows, setTotalRows] = useState(0)
  const [totalQueueQty, setTotalQueueQty] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [unavailable, setUnavailable] = useState('')

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setUnavailable('')
    try {
      const data = await listAssemblyQueue({ limit: PAGE_LIMIT, offset: nextOffset })
      setRows(data.rows ?? [])
      setTotalRows(Number(data.total_rows || 0))
      setTotalQueueQty(Number(data.total_queue_qty || 0))
      setOffset(Number(data.offset ?? nextOffset))
    } catch (e) {
      const blocked = unavailableTruth(e)
      if (blocked) setUnavailable(blocked.reason)
      else setError(e instanceof Error ? e.message : String(e))
      setRows([])
      setTotalRows(0)
      setTotalQueueQty(0)
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
        title="Очередь сборки"
        subtitle="Текущий зафиксированный срез очереди сборки по принятым данным плана"
        hotkeys="Внешних источников данных нет"
        footer={
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
        }
      >
        <div className="commandBar">
          <button onClick={() => void load(offset)} disabled={loading}>Обновить</button>
          <div className="toolbarText">Ожидаемая доступность: {totalRows ? `${formatNumber(totalQueueQty)} ед.` : 'нет строк'}</div>
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
                <th className="numCell">run</th>
                <th className="numCell">plan</th>
                <th className="numCell">plan_line</th>
                <th className="numCell">item_id</th>
                <th>Артикул</th>
                <th>Номенклатура</th>
                <th>Дата плана</th>
                <th>Период</th>
                <th className="numCell">Запланировано</th>
                <th className="numCell">Принято</th>
                <th className="numCell">Остаток</th>
                <th>Приоритет</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={12}>Загрузка...</td>
                </tr>
              ) : null}
              {!loading && !rows.length && !unavailable && !error ? (
                <tr>
                  <td colSpan={12}>Нет строк очереди на текущей правде</td>
                </tr>
              ) : null}
              {!loading && rows.map((row) => (
                <tr key={`${row.plan_id}-${row.plan_line_id}`}>
                  <td className="numCell">{row.run_id}</td>
                  <td className="numCell">{row.plan_id}</td>
                  <td className="numCell">{row.plan_line_id}</td>
                  <td className="numCell">{row.item_id}</td>
                  <td><strong>{row.item_code || `#${row.item_id}`}</strong></td>
                  <td>{row.item_name}</td>
                  <td>{formatPeriod(row.bucket_date)}</td>
                  <td>{formatPeriod(row.period_from)}–{formatPeriod(row.period_to)}</td>
                  <td className="numCell">{formatNumber(row.planned_output_qty)}</td>
                  <td className="numCell">{formatNumber(row.accepted_plan_output_qty)}</td>
                  <td className="numCell">{formatNumber(row.assembly_remaining_qty)}</td>
                  <td>{formatPriority(row.priority_key)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
