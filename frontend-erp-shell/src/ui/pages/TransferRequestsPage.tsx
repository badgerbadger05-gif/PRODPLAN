import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  coverageLabels,
  type MaterialIssueDetail,
  type TransferIssueRow,
  type TransferIssuesResponse,
} from '../../domain/productionControl'
import { api } from '../../lib/api'
import { dateRu, qty } from '../../lib/format'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { tableColumnStyle, tableMinWidth } from '../tableDoctype'
import { transferRequestColumns } from './transferRequestsDoctype'

const limit = 100

const transferStatusLabels: Record<string, string> = {
  draft: 'Черновик',
  requested: 'Заявка',
  exported: 'В 1С',
  posted: 'Собрано',
  error: 'Ошибка',
  cancelled: 'Отменено',
}

export function TransferRequestsPage() {
  const [rows, setRows] = useState<TransferIssueRow[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [detail, setDetail] = useState<MaterialIssueDetail | null>(null)
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const offsetRef = useRef(offset)

  useEffect(() => {
    offsetRef.current = offset
  }, [offset])

  const activeRow = useMemo(() => rows.find((row) => row.issue_id === activeId) ?? rows[0] ?? null, [rows, activeId])
  const canAssemble = activeRow
    ? (activeRow.can_assemble ?? (!!activeRow.exported_ref1c && activeRow.status !== 'posted'))
    : false
  const assembleDisabledReason = activeRow?.assemble_disabled_reason
    || (!activeRow ? 'Выберите заявку' : !activeRow.exported_ref1c ? 'Сначала выгрузите перемещение в 1С' : activeRow.status === 'posted' ? 'Перемещение уже собрано' : '')

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      params.set('limit', String(limit))
      params.set('offset', String(nextOffset))
      if (status) params.set('status', status)
      if (search.trim()) params.set('search', search.trim())
      const data = await api<TransferIssuesResponse>(`/v1/production-control/material-issues?${params.toString()}`)
      setRows(data.rows ?? [])
      setTotal(data.total ?? 0)
      setOffset(nextOffset)
      setActiveId((current) => {
        if (current && data.rows?.some((row) => row.issue_id === current)) return current
        return data.rows?.[0]?.issue_id ?? null
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [search, status])

  const loadDetail = useCallback(async (issueId: number) => {
    setDetail(null)
    try {
      setDetail(await api<MaterialIssueDetail>(`/v1/production-control/material-issues/${issueId}`))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  async function markAssembled() {
    if (!activeRow) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      await api(`/v1/production-control/material-issues/${activeRow.issue_id}/assembled`, {
        method: 'POST',
        body: JSON.stringify({ allow_production: false }),
      })
      setMessage(`Перемещение ${activeRow.one_c_number || activeRow.document_number} проведено, обеспечение обновлено: собрано`)
      await load(offsetRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(0)
  }, [load])

  useEffect(() => {
    if (activeRow) void loadDetail(activeRow.issue_id)
  }, [activeRow, loadDetail])

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Заявки на перемещение</div>
        <div className="runBadge">заявок: {total}</div>
      </div>

      <DocumentWindow
        title="Заявки на перемещение"
        subtitle="Непроведённые перемещения из запуска заказов и детали комплектующих к сборке"
        hotkeys="F5 Обновить · Enter Детали"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={activeRow ? 1 : 0}
            canPrev={offset > 0}
            canNext={offset + rows.length < total}
            onPrev={() => void load(Math.max(0, offset - limit))}
            onNext={() => void load(offset + limit)}
          />
        )}
      >
        <div className="commandBar">
          <button className="primary" onClick={() => void markAssembled()} disabled={!canAssemble || loading} title={!canAssemble ? assembleDisabledReason : 'Провести перемещение в 1С'}>
            Собрано
          </button>
          <button onClick={() => void load(offset)} disabled={loading}>Обновить</button>
          {!canAssemble && assembleDisabledReason && <span className="toolbarText">{assembleDisabledReason}</span>}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split">
          <div className="tablePane">
            <table className="journalTable columnFilterTable transferTable" style={{ minWidth: tableMinWidth(transferRequestColumns) }}>
              <colgroup>
                {transferRequestColumns.map((column) => (
                  <col key={column.key} style={tableColumnStyle(column)} />
                ))}
              </colgroup>
              <tbody>
                <tr>
                  <td className="checkCol"></td>
                  <td colSpan={5}>
                    <div className="columnFilterSearch">
                      <label className="columnFilterControl">
                        <span>Поиск</span>
                        <input value={search} onChange={(e) => setSearch(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void load(0) }} />
                      </label>
                      <button onClick={() => void load(0)} disabled={loading}>Найти</button>
                    </div>
                  </td>
                  <td>
                    <label className="columnFilterControl">
                      <span>Статус</span>
                      <select value={status} onChange={(e) => setStatus(e.target.value)}>
                        <option value="">Все</option>
                        <option value="draft">Черновик</option>
                        <option value="requested">Заявка</option>
                        <option value="exported">В 1С</option>
                        <option value="posted">Собрано</option>
                        <option value="error">Ошибка</option>
                      </select>
                    </label>
                  </td>
                </tr>
              </tbody>
            </table>
            <table className="journalTable transferTable" style={{ minWidth: tableMinWidth(transferRequestColumns) }}>
              <colgroup>
                {transferRequestColumns.map((column) => (
                  <col key={column.key} style={tableColumnStyle(column)} />
                ))}
              </colgroup>
              <thead>
                <tr>
                  {transferRequestColumns.map((column) => (
                    <th key={column.key} className={column.className} style={tableColumnStyle(column)}>{column.title}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.issue_id} className={row.issue_id === activeRow?.issue_id ? 'activeRow' : ''} onClick={() => setActiveId(row.issue_id)}>
                    <td className="checkCol">
                      <input
                        type="checkbox"
                        checked={row.issue_id === activeRow?.issue_id}
                        onChange={() => setActiveId(row.issue_id)}
                        onClick={(e) => e.stopPropagation()}
                        aria-label={`Выбрать ${row.document_number}`}
                      />
                    </td>
                    <td className="orderCell">
                      <strong>{row.document_number}</strong>
                      <span>{dateRu(row.created_at) || '—'} · строк {row.lines_count ?? 0}</span>
                    </td>
                    <td className="orderCell">
                      <strong>{row.order_number}</strong>
                      <span>{row.order_ref1c ? row.order_ref1c.slice(0, 8) : 'заказ без Ref_Key'}</span>
                    </td>
                    <td className="itemCell">
                      <strong>{row.item_name}</strong>
                      <span>{row.item_article || row.item_code || ''}</span>
                    </td>
                    <td className="numCell">
                      <strong>{qty(row.remaining_qty || row.quantity)}</strong>
                      <span>{row.unit || ''}</span>
                    </td>
                    <td>
                      <strong>{row.one_c_number || (row.exported_ref1c ? row.exported_ref1c.slice(0, 8) : '—')}</strong>
                      <span>{row.one_c_number && row.exported_ref1c ? row.exported_ref1c.slice(0, 8) : dateRu(row.exported_at) || row.export_error || ''}</span>
                    </td>
                    <td>
                      <span className={`pill ${row.status === 'posted' ? 'assembled' : row.status === 'error' ? 'shortage' : 'to_move'}`}>
                        {transferStatusLabels[row.status] || row.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <aside className="detailPane">
            <h2>Детали к сборке</h2>
            {activeRow ? (
              <>
                <div className="detailTitle">{activeRow.item_name}</div>
                <div className="detailMeta">{activeRow.one_c_number || activeRow.document_number} · {activeRow.order_number}</div>
                <div className="detailGrid">
                  <span>Статус</span><strong>{transferStatusLabels[activeRow.status] || activeRow.status}</strong>
                  <span>Обеспечение</span><strong>{coverageLabels[String(activeRow.line_status || '')] || activeRow.line_status || '—'}</strong>
                  <span>Отправитель</span><strong>{activeRow.source_warehouse_ref1c || '—'}</strong>
                  <span>Получатель</span><strong>{activeRow.warehouse_ref1c || '—'}</strong>
                  <span>Номер 1С</span><strong>{activeRow.one_c_number || '—'}</strong>
                  <span>Ref 1С</span><strong>{activeRow.exported_ref1c || '—'}</strong>
                  {activeRow.export_error && <span>Ошибка 1С</span>}
                  {activeRow.export_error && <strong>{activeRow.export_error}</strong>}
                </div>
                <h3>Комплектующие</h3>
                <div className="materialsList">
                  {(detail?.lines ?? []).map((line) => (
                    <div className="materialRow" key={line.line_id}>
                      <div>
                        <strong>{line.item_name}</strong>
                        <span>{line.item_article || line.item_code}</span>
                      </div>
                      <div className="matNums">
                        <span>нужно {qty(line.required_qty)}</span>
                        <span>выдано {qty(line.issued_qty)}</span>
                      </div>
                      <span className={`miniPill ${line.line_status === 'issued' ? 'assembled' : 'to_move'}`}>{line.line_status || 'planned'}</span>
                    </div>
                  ))}
                  {!detail?.lines?.length && <div className="emptyDetail">Комплектующие не загружены</div>}
                </div>
              </>
            ) : (
              <div className="emptyDetail">Выберите заявку</div>
            )}
          </aside>
        </div>
      </DocumentWindow>
    </main>
  )
}
