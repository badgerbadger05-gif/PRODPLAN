import { useEffect, useState } from 'react'
import type { ExecutionJournalResponse, PeriodPlan, PeriodPlanMatrix } from '../../domain/planning'
import {
  coverageClass,
  flowClass,
  flowLabel,
  periodPlanStatusClass,
  periodPlanStatusLabel,
} from '../../domain/planning'
import { dateRu, dateTimeRu, qty } from '../../lib/format'
import {
  allocatePurchases,
  allocateRework,
  bulkUpsertPeriodPlanLines,
  createMrpSnapshot,
  createPeriodPlan,
  fixPeriodPlan,
  getExecutionJournal,
  getPeriodPlanMatrix,
  listPeriodPlans,
} from '../../services/periodPlan'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const PLAN_LIMIT = 50

type Tab = 'matrix' | 'journal'

// ── Create-plan form state ────────────────────────────────────────────────────

function nextFriday(offset = 0) {
  const d = new Date()
  const dow = d.getDay()
  d.setDate(d.getDate() + ((5 - dow + 7) % 7) + offset * 7)
  return d.toISOString().slice(0, 10)
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function bucketLabel(iso: string) {
  return dateRu(iso).slice(0, 5) // DD.MM
}

// ── Main component ────────────────────────────────────────────────────────────

export function PeriodPlanPage() {
  // List state
  const [plans, setPlans] = useState<PeriodPlan[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  // Loading flags
  const [loading, setLoading] = useState(false)
  const [acting, setActing] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  // Detail state
  const [tab, setTab] = useState<Tab>('matrix')
  const [matrix, setMatrix] = useState<PeriodPlanMatrix | null>(null)
  const [matrixLoading, setMatrixLoading] = useState(false)
  const [matrixError, setMatrixError] = useState('')
  const [dirty, setDirty] = useState<Record<number, Record<string, number>>>({})
  const [saving, setSaving] = useState(false)

  const [journal, setJournal] = useState<ExecutionJournalResponse | null>(null)
  const [journalLoading, setJournalLoading] = useState(false)
  const [journalError, setJournalError] = useState('')
  const [journalFlow, setJournalFlow] = useState('')
  const [expandedReq, setExpandedReq] = useState<number | null>(null)
  const [lastRunId, setLastRunId] = useState<number | null>(null)

  // Create-plan form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newFrom, setNewFrom] = useState(nextFriday(0))
  const [newTo, setNewTo] = useState(nextFriday(3))
  const [creating, setCreating] = useState(false)

  const selected = plans.find((p) => p.id === selectedId) ?? null

  // ── Load list ───────────────────────────────────────────────────────────────

  async function loadList(nextOffset = offset) {
    setLoading(true)
    setError('')
    try {
      const data = await listPeriodPlans({ limit: PLAN_LIMIT, offset: nextOffset })
      setPlans(data.rows ?? [])
      setTotal(data.total ?? 0)
      setOffset(nextOffset)
      if (!selectedId && data.rows?.length) {
        setSelectedId(data.rows[0].id)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  // ── Load matrix ─────────────────────────────────────────────────────────────

  async function loadMatrix(planId: number) {
    setMatrixLoading(true)
    setMatrixError('')
    setDirty({})
    try {
      const data = await getPeriodPlanMatrix(planId)
      setMatrix(data)
    } catch (e) {
      setMatrixError(e instanceof Error ? e.message : String(e))
    } finally {
      setMatrixLoading(false)
    }
  }

  // ── Load journal ─────────────────────────────────────────────────────────────

  async function loadJournal(planId: number, flow = journalFlow, runId?: number) {
    setJournalLoading(true)
    setJournalError('')
    try {
      const data = await getExecutionJournal(planId, { flow: flow || undefined, run_id: runId })
      setJournal(data)
      setLastRunId(data.run_id)
    } catch (e) {
      setJournalError(e instanceof Error ? e.message : String(e))
      setJournal(null)
    } finally {
      setJournalLoading(false)
    }
  }

  // ── Side effects ────────────────────────────────────────────────────────────

  useEffect(() => {
    void loadList(0)
  }, [])

  useEffect(() => {
    if (!selectedId) return
    setMatrix(null)
    setJournal(null)
    setLastRunId(null)
    setDirty({})
    setExpandedReq(null)
    if (tab === 'matrix') void loadMatrix(selectedId)
    if (tab === 'journal') void loadJournal(selectedId)
  }, [selectedId])

  useEffect(() => {
    if (!selectedId) return
    if (tab === 'matrix' && !matrix) void loadMatrix(selectedId)
    if (tab === 'journal' && !journal) void loadJournal(selectedId)
  }, [tab])

  // ── Actions ─────────────────────────────────────────────────────────────────

  async function handleCreate() {
    if (!newName.trim() || !newFrom || !newTo) return
    setCreating(true)
    setError('')
    try {
      await createPeriodPlan({ name: newName.trim(), period_from: newFrom, period_to: newTo })
      setShowCreate(false)
      setNewName('')
      await loadList(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  async function handleFix() {
    if (!selected) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      await fixPeriodPlan(selected.id)
      setMessage('План зафиксирован')
      await loadList(offset)
      if (selectedId) await loadMatrix(selectedId)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleSnapshot() {
    if (!selected) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      const result = await createMrpSnapshot(selected.id)
      setLastRunId(result.run_id)
      setMessage(`MRP-снимок создан: run #${result.run_id}, требований: ${result.requirement_count}, закупок: ${result.purchase_count}, переработок: ${result.rework_count}`)
      setTab('journal')
      await loadJournal(selected.id, journalFlow, result.run_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleAllocate() {
    if (!lastRunId) return
    setActing(true)
    setError('')
    setMessage('')
    try {
      const [p, r] = await Promise.all([
        allocatePurchases(lastRunId),
        allocateRework(lastRunId),
      ])
      setMessage(`Аллокация: закупки ${p.updated_count} строк, переработки ${r.updated_count} строк`)
      if (selectedId) await loadJournal(selectedId, journalFlow, lastRunId)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActing(false)
    }
  }

  async function handleSaveMatrix() {
    if (!selectedId || !matrix) return
    const entries: Array<{ item_id: number; bucket_date: string; qty: number }> = []
    for (const [itemId, bucketMap] of Object.entries(dirty)) {
      for (const [bucket_date, qty] of Object.entries(bucketMap)) {
        entries.push({ item_id: Number(itemId), bucket_date, qty })
      }
    }
    if (!entries.length) return
    setSaving(true)
    setMatrixError('')
    try {
      await bulkUpsertPeriodPlanLines(selectedId, entries)
      setDirty({})
      await loadMatrix(selectedId)
    } catch (e) {
      setMatrixError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function handleCellChange(itemId: number, bucket: string, value: string) {
    const num = parseFloat(value) || 0
    setDirty((prev) => ({
      ...prev,
      [itemId]: { ...(prev[itemId] ?? {}), [bucket]: num },
    }))
  }

  function cellValue(row: { item_id: number; buckets: Record<string, number> }, bucket: string) {
    const d = dirty[row.item_id]?.[bucket]
    return d !== undefined ? d : (row.buckets[bucket] ?? 0)
  }

  const isDraft = selected?.status === 'draft'
  const isFixed = selected?.status === 'fixed'
  const hasDirty = Object.keys(dirty).length > 0

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + plans.length, total)

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование / Период план</div>
        <div className="runBadge">Планов: {total}</div>
      </div>

      <DocumentWindow
        title="Период план"
        subtitle="Фиксированный горизонт потребностей → MRP снимок → журнал исполнения"
        hotkeys="F5 Обновить · Enter Открыть"
        footer={(
          <StatusBar
            loading={loading || acting}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={selected ? 1 : 0}
            canPrev={offset > 0}
            canNext={offset + plans.length < total}
            onPrev={() => void loadList(Math.max(0, offset - PLAN_LIMIT))}
            onNext={() => void loadList(offset + PLAN_LIMIT)}
          />
        )}
      >
        <div className="commandBar">
          <button className="primary" onClick={() => setShowCreate(true)} disabled={acting}>Новый план</button>
          <button onClick={() => void loadList()} disabled={loading || acting}>Обновить</button>
          <div className="barSeparator" />
          {isDraft && (
            <button onClick={() => void handleFix()} disabled={acting || !selected}>Зафиксировать</button>
          )}
          {isFixed && (
            <button className="primary" onClick={() => void handleSnapshot()} disabled={acting || !selected}>MRP снимок</button>
          )}
          {lastRunId && (
            <button onClick={() => void handleAllocate()} disabled={acting}>Ре-аллокация</button>
          )}
          {hasDirty && tab === 'matrix' && (
            <>
              <div className="barSeparator" />
              <button className="primary" onClick={() => void handleSaveMatrix()} disabled={saving}>Сохранить</button>
              <button onClick={() => { setDirty({}); if (selectedId) void loadMatrix(selectedId) }} disabled={saving}>Отмена</button>
            </>
          )}
          {selected && lastRunId && (
            <span className="toolbarText" style={{ marginLeft: 6 }}>Run #{lastRunId}</span>
          )}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        {/* Create plan form */}
        {showCreate && (
          <div className="requisites" style={{ gridTemplateColumns: 'minmax(260px,1fr) 160px 160px auto auto' }}>
            <label>
              <span>Название плана</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Например: МАЙ 2026" />
            </label>
            <label>
              <span>Период с</span>
              <input type="date" value={newFrom} onChange={(e) => setNewFrom(e.target.value)} />
            </label>
            <label>
              <span>Период по</span>
              <input type="date" value={newTo} onChange={(e) => setNewTo(e.target.value)} />
            </label>
            <button className="primary" style={{ alignSelf: 'end' }} onClick={() => void handleCreate()} disabled={creating || !newName.trim()}>Создать</button>
            <button style={{ alignSelf: 'end' }} onClick={() => setShowCreate(false)}>Отмена</button>
          </div>
        )}

        {/* Split: plan list | detail */}
        <div className="split" style={{ gridTemplateColumns: '290px 1fr' }}>
          {/* Plan list */}
          <div className="tablePane" style={{ minWidth: 0 }}>
            <table className="journalTable" style={{ minWidth: 0, width: '100%', tableLayout: 'fixed' }}>
              <thead>
                <tr>
                  <th>Название</th>
                  <th style={{ width: 100 }}>Статус</th>
                  <th style={{ width: 90 }}>Период</th>
                </tr>
              </thead>
              <tbody>
                {plans.map((plan) => (
                  <tr
                    key={plan.id}
                    className={plan.id === selectedId ? 'activeRow' : ''}
                    onClick={() => setSelectedId(plan.id)}
                  >
                    <td>
                      <strong style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{plan.name}</strong>
                    </td>
                    <td><span className={`pill ${periodPlanStatusClass(plan.status)}`}>{periodPlanStatusLabel(plan.status)}</span></td>
                    <td><span className="muted">{dateRu(plan.period_from).slice(0, 5)}–{dateRu(plan.period_to).slice(0, 5)}</span></td>
                  </tr>
                ))}
                {!loading && !plans.length && (
                  <tr><td colSpan={3}><div className="emptyDetail">Нет планов</div></td></tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Detail */}
          <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            {!selected ? (
              <div className="detailPane"><div className="emptyDetail">Выберите план из списка</div></div>
            ) : (
              <>
                {/* Plan meta strip */}
                <div className="mrpSummaryStrip" style={{ gridTemplateColumns: 'repeat(5, minmax(100px,1fr))' }}>
                  <div className="metricCell">
                    <span>Название</span>
                    <strong style={{ fontSize: 14, marginTop: 4 }}>{selected.name}</strong>
                  </div>
                  <div className="metricCell">
                    <span>Статус</span>
                    <strong><span className={`pill ${periodPlanStatusClass(selected.status)}`}>{periodPlanStatusLabel(selected.status)}</span></strong>
                  </div>
                  <div className="metricCell">
                    <span>Период с</span>
                    <strong>{dateRu(selected.period_from)}</strong>
                  </div>
                  <div className="metricCell">
                    <span>Период по</span>
                    <strong>{dateRu(selected.period_to)}</strong>
                  </div>
                  <div className="metricCell">
                    <span>Зафиксирован</span>
                    <strong>{selected.fixed_at ? dateTimeRu(selected.fixed_at) : '—'}</strong>
                    {selected.fixed_by && <em style={{ fontStyle: 'normal', color: 'var(--muted)', fontSize: 11 }}>{selected.fixed_by}</em>}
                  </div>
                </div>

                {/* Tabs */}
                <div className="tabsBar">
                  <button className={tab === 'matrix' ? 'activeTab' : ''} onClick={() => setTab('matrix')}>Матрица</button>
                  <button className={tab === 'journal' ? 'activeTab' : ''} onClick={() => setTab('journal')}>Журнал исполнения</button>
                </div>

                {/* Matrix tab */}
                {tab === 'matrix' && (
                  <div className="tablePane resultTablePane" style={{ flex: 1 }}>
                    {matrixLoading && <div className="hintLine">Загрузка матрицы…</div>}
                    {matrixError && <div className="errorLine">{matrixError}</div>}
                    {matrix && (
                      <table className="journalTable" style={{ minWidth: `${460 + matrix.buckets.length * 90}px`, tableLayout: 'fixed' }}>
                        <thead>
                          <tr>
                            <th style={{ width: 74 }}>Код</th>
                            <th style={{ width: 360 }}>Номенклатура</th>
                            <th style={{ width: 96, textAlign: 'right' }}>Итого</th>
                            {matrix.buckets.map((b) => (
                              <th key={b} style={{ width: 90, textAlign: 'right' }}>{bucketLabel(b)}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {matrix.rows.map((row) => (
                            <tr key={row.item_id}>
                              <td><span className="muted">{row.item_code}</span></td>
                              <td>
                                <strong>{row.item_name}</strong>
                                {row.item_article && <span className="muted">{row.item_article}</span>}
                              </td>
                              <td className="numCell">
                                <strong>{qty(
                                  matrix.buckets.reduce((s, b) => s + cellValue(row, b), 0),
                                )}</strong>
                              </td>
                              {matrix.buckets.map((b) => {
                                const locked = row.locked_buckets[b] !== undefined
                                const val = cellValue(row, b)
                                return (
                                  <td key={b} className="weekPlanCell">
                                    {isDraft && !locked ? (
                                      <input
                                        type="number"
                                        min={0}
                                        step={1}
                                        value={val || ''}
                                        placeholder="0"
                                        onChange={(e) => handleCellChange(row.item_id, b, e.target.value)}
                                      />
                                    ) : (
                                      <span style={{ display: 'block', textAlign: 'right', paddingRight: 4, color: locked ? 'var(--muted)' : undefined }}>
                                        {val ? qty(val) : <span className="muted">—</span>}
                                      </span>
                                    )}
                                  </td>
                                )
                              })}
                            </tr>
                          ))}
                          {!matrix.rows.length && (
                            <tr><td colSpan={3 + matrix.buckets.length}><div className="emptyDetail">Матрица пуста — добавьте номенклатуру</div></td></tr>
                          )}
                        </tbody>
                      </table>
                    )}
                  </div>
                )}

                {/* Journal tab */}
                {tab === 'journal' && (
                  <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                    {/* Journal toolbar */}
                    <div className="commandBar">
                      <button onClick={() => void loadJournal(selected.id, journalFlow, lastRunId ?? undefined)} disabled={journalLoading}>Обновить</button>
                      <div className="barSeparator" />
                      <label className="inlineControl">
                        <span>Поток</span>
                        <select value={journalFlow} onChange={(e) => { setJournalFlow(e.target.value); void loadJournal(selected.id, e.target.value, lastRunId ?? undefined) }}>
                          <option value="">Все</option>
                          <option value="production">Производство</option>
                          <option value="purchase">Закупка</option>
                          <option value="rework">Переработка</option>
                        </select>
                      </label>
                      {journal && (
                        <>
                          <div className="barSeparator" />
                          <span className="toolbarText">Покрыто: {journal.summary.fully_covered} / {journal.summary.total_items}</span>
                          {journal.summary.not_covered > 0 && (
                            <span style={{ color: 'var(--red)' }}>Не покрыто: {journal.summary.not_covered}</span>
                          )}
                          {journal.summary.partially_covered > 0 && (
                            <span style={{ color: 'var(--orange)' }}>Частично: {journal.summary.partially_covered}</span>
                          )}
                        </>
                      )}
                    </div>

                    {journalLoading && <div className="hintLine">Загрузка журнала…</div>}
                    {journalError && <div className="errorLine">{journalError}</div>}
                    {!journal && !journalLoading && !journalError && (
                      <div className="emptyDetail" style={{ margin: 16 }}>
                        MRP-снимок не создан. Зафиксируйте план и нажмите «MRP снимок».
                      </div>
                    )}

                    {journal && (
                      <div className="tablePane resultTablePane" style={{ flex: 1 }}>
                        <table className="journalTable" style={{ minWidth: 860 }}>
                          <thead>
                            <tr>
                              <th style={{ width: 74 }}>Код</th>
                              <th style={{ width: 300 }}>Номенклатура</th>
                              <th style={{ width: 104 }}>Поток</th>
                              <th style={{ width: 52, textAlign: 'center' }}>Ур.</th>
                              <th className="numCell">Валовый</th>
                              <th className="numCell">Нетто</th>
                              <th className="numCell">Покрыто</th>
                              <th className="numCell">Остаток</th>
                              <th style={{ width: 80, textAlign: 'center' }}>%</th>
                              <th style={{ width: 64, textAlign: 'center' }}>Заданий</th>
                            </tr>
                          </thead>
                          <tbody>
                            {journal.rows.map((row) => (
                              <>
                                <tr
                                  key={row.req_id}
                                  className={expandedReq === row.req_id ? 'activeRow' : ''}
                                  style={{ cursor: row.work_items.length ? 'pointer' : undefined }}
                                  onClick={() => setExpandedReq(expandedReq === row.req_id ? null : row.req_id)}
                                >
                                  <td><span className="muted">{row.item_code}</span></td>
                                  <td><strong>{row.item_name}</strong></td>
                                  <td><span className={`miniPill ${flowClass(row.flow)}`}>{flowLabel(row.flow)}</span></td>
                                  <td style={{ textAlign: 'center' }}>{row.bom_level}</td>
                                  <td className="numCell"><strong>{qty(row.gross_qty)}</strong></td>
                                  <td className="numCell"><strong>{qty(row.net_qty)}</strong></td>
                                  <td className="numCell">{qty(row.covered_qty)}</td>
                                  <td className="numCell" style={{ color: row.remaining_qty > 0 ? 'var(--red)' : undefined }}>
                                    {row.remaining_qty > 0 ? qty(row.remaining_qty) : '—'}
                                  </td>
                                  <td style={{ textAlign: 'center' }}>
                                    <span className={`miniPill ${coverageClass(row.coverage_pct)}`}>{row.coverage_pct}%</span>
                                  </td>
                                  <td style={{ textAlign: 'center' }}>
                                    {row.work_items.length ? (
                                      <span style={{ fontWeight: 700 }}>{row.work_items.length}</span>
                                    ) : <span className="muted">—</span>}
                                  </td>
                                </tr>
                                {expandedReq === row.req_id && row.work_items.map((wi, i) => (
                                  <tr key={i} style={{ background: '#f8fbff' }}>
                                    <td />
                                    <td colSpan={2} style={{ paddingLeft: 24 }}>
                                      <span className="muted">{wi.type === 'production_order' ? `Заказ ${wi.order_number || '#' + wi.order_id}` : wi.type === 'planned_purchase' ? `Закупка #${wi.purchase_id}` : `Переработка #${wi.rework_id}`}</span>
                                    </td>
                                    <td />
                                    <td />
                                    <td className="numCell"><strong>{qty(wi.qty)}</strong></td>
                                    <td className="numCell">
                                      {wi.remaining_qty !== undefined ? qty(wi.remaining_qty) : '—'}
                                    </td>
                                    <td />
                                    <td style={{ textAlign: 'center' }}>
                                      {wi.need_date ? <span className="muted">{dateRu(wi.need_date)}</span> : '—'}
                                    </td>
                                    <td />
                                  </tr>
                                ))}
                              </>
                            ))}
                            {!journal.rows.length && (
                              <tr><td colSpan={10}><div className="emptyDetail">Нет данных по выбранному фильтру</div></td></tr>
                            )}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </DocumentWindow>
    </main>
  )
}
