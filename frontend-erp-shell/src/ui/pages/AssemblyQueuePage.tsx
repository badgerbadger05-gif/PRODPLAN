import { useCallback, useEffect, useState } from 'react'
import { dateRu, qty } from '../../lib/format'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { listAssemblyQueue } from '../../services/assemblyQueue'
import type { AssemblyQueueRow } from '../../domain/assemblyQueue'

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
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await listAssemblyQueue()
      setRows(data.rows ?? [])
      setTotalRows(Number(data.total_rows || 0))
      setTotalQueueQty(Number(data.total_queue_qty || 0))
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
        title="Очередь сборки"
        subtitle="Текущий зафиксированный срез очереди сборки по принятым данным плана"
        hotkeys="Внешних источников данных нет"
        footer={
          <StatusBar
            loading={loading}
            visibleFrom={rows.length ? 1 : 0}
            visibleTo={rows.length}
            total={totalRows}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        }
      >
        <div className="commandBar">
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
          <div className="toolbarText">Ожидаемая доступность: {totalRows ? `${formatNumber(totalQueueQty)} ед.` : 'нет строк'}</div>
        </div>

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
              {!loading && !rows.length ? (
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
