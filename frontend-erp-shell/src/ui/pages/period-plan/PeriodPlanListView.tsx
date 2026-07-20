import { useCallback, useEffect, useState } from 'react'
import type { PeriodPlan } from '../../../domain/planning'
import { periodPlanStatusClass, periodPlanStatusLabel } from '../../../domain/planning'
import { dateRu, dateTimeRu } from '../../../lib/format'
import { createPeriodPlan, deletePeriodPlan, listPeriodPlans } from '../../../services/periodPlan'
import { DocumentWindow } from '../../layout/DocumentWindow'
import { StatusBar } from '../../layout/StatusBar'
import { tableColumnStyle, tableMinWidth, type TableColumnDoctype } from '../../tableDoctype'
import { nextFriday, type SortDir } from './helpers'

const PLAN_LIMIT = 50

interface ListViewProps {
  onOpenPlan: (id: number) => void
}

type SortKey = 'name' | 'status' | 'period_from' | 'period_to' | 'fixed_at' | 'created_at'

const periodPlanListColumns = [
  { key: 'name', title: 'Название', width: 240, minWidth: 240, grow: false, sortable: true },
  { key: 'status', title: 'Статус', width: 110, minWidth: 110, grow: false, sortable: true },
  { key: 'period_from', title: 'Период', width: 180, minWidth: 180, grow: false, sortable: true },
  { key: 'fixed_at', title: 'Зафиксирован', width: 140, minWidth: 140, grow: false, sortable: true },
  { key: 'fixed_by', title: 'Кем', width: 110, minWidth: 110, grow: false, sortable: false },
  { key: 'created_at', title: 'Создан', width: 140, minWidth: 140, grow: false, sortable: true },
  { key: 'line_count', title: 'Строк', width: 64, minWidth: 64, grow: false, align: 'right', sortable: false },
  { key: 'comment', title: 'Комментарий', minWidth: 240, grow: true, sortable: false },
] as const satisfies TableColumnDoctype[]

export function PeriodPlanListView({ onOpenPlan }: ListViewProps) {
  const [plans, setPlans] = useState<PeriodPlan[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)

  // Filters
  const [filterStatus, setFilterStatus] = useState<string>('')
  const [filterFrom, setFilterFrom] = useState<string>('')
  const [filterTo, setFilterTo] = useState<string>('')
  const [filterCreatedBy, setFilterCreatedBy] = useState<string>('')
  const [sortBy, setSortBy] = useState<SortKey>('period_from')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newFrom, setNewFrom] = useState(nextFriday(0))
  const [newTo, setNewTo] = useState(nextFriday(3))
  const [newComment, setNewComment] = useState('')
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const selected = plans.find((p) => p.id === selectedId) ?? null
  const canDelete = selected?.status !== 'archived' // backend will reject if there are SUCCESS MRP runs
  const dateOrderInvalid = !!(newFrom && newTo && newFrom > newTo)

  const loadList = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    try {
      const data = await listPeriodPlans({
        limit: PLAN_LIMIT,
        offset: nextOffset,
        status: filterStatus || undefined,
        period_from: filterFrom || undefined,
        period_to: filterTo || undefined,
        created_by: filterCreatedBy || undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
      })
      setPlans(data.rows ?? [])
      setTotal(data.total ?? 0)
      setOffset(nextOffset)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [filterStatus, filterFrom, filterTo, filterCreatedBy, sortBy, sortDir])

  useEffect(() => { void loadList(0) }, [loadList])

  // Keyboard hotkeys: F5 refresh, Enter open
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null
      const inInput = !!target && /^(INPUT|TEXTAREA|SELECT)$/i.test(target.tagName)
      if (e.key === 'F5') {
        e.preventDefault()
        void loadList(offset)
        return
      }
      if (e.key === 'Enter' && !inInput && selected) {
        e.preventDefault()
        onOpenPlan(selected.id)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [loadList, offset, selected, onOpenPlan])

  async function handleCreate() {
    if (!newName.trim() || !newFrom || !newTo) return
    if (dateOrderInvalid) {
      setError('Дата окончания периода не может быть раньше даты начала')
      return
    }
    setCreating(true)
    setError('')
    try {
      const created = await createPeriodPlan({
        name: newName.trim(),
        period_from: newFrom,
        period_to: newTo,
        comment: newComment.trim() || null,
      })
      setShowCreate(false)
      setNewName('')
      setNewComment('')
      await loadList(0)
      if (created?.id) onOpenPlan(created.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete() {
    if (!selected) return
    if (!confirm(`Удалить план «${selected.name}»?`)) return
    setDeleting(true)
    setError('')
    setMessage('')
    try {
      await deletePeriodPlan(selected.id)
      setMessage(`План «${selected.name}» удалён`)
      setSelectedId(null)
      await loadList(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  function toggleSort(key: SortKey) {
    if (sortBy === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortBy(key)
      setSortDir('desc')
    }
  }

  function sortArrow(key: SortKey) {
    if (sortBy !== key) return ''
    return sortDir === 'asc' ? ' ▲' : ' ▼'
  }

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + plans.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование / Планирование выпуска</div>
        <div className="runBadge">Планов: {total}</div>
      </div>

      <DocumentWindow
        title="Планирование выпуска"
        subtitle="Список планов производства на выбранный период"
        hotkeys="F5 Обновить · Enter Открыть"
        footer={(
          <StatusBar
            loading={loading || deleting}
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
          <button className="primary" onClick={() => setShowCreate(true)} disabled={creating}>Новый план</button>
          <button onClick={() => void loadList(offset)} disabled={loading}>Обновить</button>
          <div className="barSeparator" />
          <button onClick={() => selected && onOpenPlan(selected.id)} disabled={!selected}>Открыть</button>
          {canDelete && (
            <button onClick={() => void handleDelete()} disabled={deleting || !selected} style={{ color: 'var(--red)' }}>
              Удалить
            </button>
          )}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        {showCreate && (
          <div className="requisites" style={{ gridTemplateColumns: 'minmax(220px,1fr) 150px 150px minmax(220px,1fr) auto auto' }}>
            <label>
              <span>Название плана</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Например: МАЙ 2026" autoFocus />
            </label>
            <label>
              <span>Период с (пятница)</span>
              <input type="date" value={newFrom} onChange={(e) => setNewFrom(e.target.value)} />
            </label>
            <label>
              <span>Период по</span>
              <input type="date" value={newTo} onChange={(e) => setNewTo(e.target.value)} style={dateOrderInvalid ? { borderColor: 'var(--red)' } : undefined} />
            </label>
            <label>
              <span>Комментарий</span>
              <input value={newComment} onChange={(e) => setNewComment(e.target.value)} placeholder="опционально" />
            </label>
            <button
              className="primary"
              style={{ alignSelf: 'end' }}
              onClick={() => void handleCreate()}
              disabled={creating || !newName.trim() || dateOrderInvalid}
              title={dateOrderInvalid ? 'Период «по» раньше периода «с»' : undefined}
            >
              Создать
            </button>
            <button style={{ alignSelf: 'end' }} onClick={() => { setShowCreate(false); setError('') }}>Отмена</button>
            {dateOrderInvalid && (
              <div style={{ gridColumn: '1 / -1', color: 'var(--red)', fontSize: 11 }}>
                Дата окончания периода не может быть раньше даты начала. Шаг по неделям — пятницы.
              </div>
            )}
          </div>
        )}

        <div className="tablePane resultTablePane" style={{ flex: 1 }}>
          <table className="journalTable columnFilterTable" style={{ minWidth: tableMinWidth(periodPlanListColumns) }}>
            <colgroup>
              {periodPlanListColumns.map((column) => (
                <col key={column.key} style={tableColumnStyle(column)} />
              ))}
            </colgroup>
            <tbody>
              <tr>
                <td></td>
                <td>
                  <label className="columnFilterControl">
                    <span>Статус</span>
                    <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
                      <option value="">Все</option>
                      <option value="draft">Черновик</option>
                      <option value="fixed">Зафиксирован</option>
                      <option value="archived">Архив</option>
                    </select>
                  </label>
                </td>
                <td colSpan={2}>
                  <div className="columnFilterRange">
                    <label className="columnFilterControl">
                      <span>Период с</span>
                      <input type="date" value={filterFrom} onChange={(e) => setFilterFrom(e.target.value)} />
                    </label>
                    <label className="columnFilterControl">
                      <span>Период по</span>
                      <input type="date" value={filterTo} onChange={(e) => setFilterTo(e.target.value)} />
                    </label>
                  </div>
                </td>
                <td colSpan={2}>
                  <label className="columnFilterControl">
                    <span>Автор</span>
                    <input value={filterCreatedBy} onChange={(e) => setFilterCreatedBy(e.target.value)} placeholder="любая часть имени" />
                  </label>
                </td>
                <td></td>
                <td>
                  <button
                    className="columnFilterButton"
                    onClick={() => { setFilterStatus(''); setFilterFrom(''); setFilterTo(''); setFilterCreatedBy('') }}
                  >
                    Сбросить
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <table className="journalTable" style={{ minWidth: tableMinWidth(periodPlanListColumns), tableLayout: 'fixed' }}>
            <colgroup>
              {periodPlanListColumns.map((column) => (
                <col key={column.key} style={tableColumnStyle(column)} />
              ))}
            </colgroup>
            <thead>
              <tr>
                {periodPlanListColumns.map((column) => (
                  <th key={column.key} style={tableColumnStyle(column)}>
                    {column.sortable ? (
                      <button type="button" className="tableSortButton" onClick={() => toggleSort(column.key as SortKey)}>
                        {column.title}{sortArrow(column.key as SortKey)}
                      </button>
                    ) : column.title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {plans.map((plan) => (
                <tr
                  key={plan.id}
                  className={plan.id === selectedId ? 'activeRow' : ''}
                  style={{ cursor: 'pointer', opacity: plan.status === 'archived' ? 0.62 : undefined }}
                  onClick={() => setSelectedId(plan.id === selectedId ? null : plan.id)}
                  onDoubleClick={() => onOpenPlan(plan.id)}
                >
                  <td><strong>{plan.name}</strong></td>
                  <td><span className={`pill ${periodPlanStatusClass(plan.status)}`}>{periodPlanStatusLabel(plan.status)}</span></td>
                  <td><span className="muted">{dateRu(plan.period_from)} — {dateRu(plan.period_to)}</span></td>
                  <td><span className="muted">{plan.fixed_at ? dateTimeRu(plan.fixed_at) : '—'}</span></td>
                  <td><span className="muted">{plan.fixed_by ?? plan.created_by ?? '—'}</span></td>
                  <td><span className="muted">{plan.created_at ? dateTimeRu(plan.created_at) : '—'}</span></td>
                  <td style={{ textAlign: 'right' }}><strong>{plan.line_count ?? 0}</strong></td>
                  <td><span className="muted">{plan.comment ?? '—'}</span></td>
                </tr>
              ))}
              {!loading && !plans.length && (
                <tr><td colSpan={8}><div className="emptyDetail">Нет планов</div></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
