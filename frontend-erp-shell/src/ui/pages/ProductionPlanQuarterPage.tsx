import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { NomenclatureSearchItem, PlanChange, PlanMatrixRow, WeekInfo } from '../../domain/productionPlan'
import { isoToday, localIsoDate, qty } from '../../lib/format'
import { bulkUpsertPlan, deletePlanRow, ensurePlanItem, getPlanMatrix, searchNomenclature } from '../../services/productionPlan'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

type RowVm = PlanMatrixRow & {
  weekValues: Record<string, number>
}

const pageSize = 50

function rollingThreeMonths(startIso: string) {
  const start = new Date(`${startIso}T00:00:00`)
  const end = new Date(start)
  end.setMonth(end.getMonth() + 3)
  const diffMs = end.getTime() - start.getTime()
  return Math.max(1, Math.ceil(diffMs / 86400000))
}

function isoWeekKey(dateIso: string) {
  const date = new Date(`${dateIso}T00:00:00`)
  const tmp = new Date(date)
  tmp.setDate(tmp.getDate() + 3 - ((tmp.getDay() + 6) % 7))
  const week1 = new Date(tmp.getFullYear(), 0, 4)
  const week = 1 + Math.round(((tmp.getTime() - week1.getTime()) / 86400000 - 3 + ((week1.getDay() + 6) % 7)) / 7)
  return `${tmp.getFullYear()}-W${String(week).padStart(2, '0')}`
}

function buildWeeks(dates: string[]): WeekInfo[] {
  const map = new Map<string, WeekInfo>()
  dates.forEach((dateIso) => {
    const key = isoWeekKey(dateIso)
    const date = new Date(`${dateIso}T00:00:00`)
    const friday = new Date(date)
    friday.setDate(date.getDate() + (5 - date.getDay()))
    if (!map.has(key)) {
      map.set(key, { key, label: `${key} пт ${localIsoDate(friday).slice(8, 10)}.${localIsoDate(friday).slice(5, 7)}`, friday: localIsoDate(friday), dates: [] })
    }
    map.get(key)!.dates.push(dateIso)
  })
  return Array.from(map.values()).sort((a, b) => a.dates[0].localeCompare(b.dates[0]))
}

export function ProductionPlanQuarterPage() {
  const [startDate, setStartDate] = useState(isoToday())
  const initialStartDate = useRef(startDate)
  const [rows, setRows] = useState<RowVm[]>([])
  const [weeks, setWeeks] = useState<WeekInfo[]>([])
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [pending, setPending] = useState<PlanChange[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [searchRows, setSearchRows] = useState<NomenclatureSearchItem[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [searching, setSearching] = useState(false)
  const [deletingId, setDeletingId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const visibleFrom = total ? (page - 1) * pageSize + 1 : 0
  const visibleTo = Math.min(page * pageSize, total)

  const quarterSum = useMemo(() => rows.reduce((sum, row) => sum + Number(row.month_plan || 0), 0), [rows])

  const load = useCallback(async (nextPage: number, nextStartDate: string) => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const days = rollingThreeMonths(nextStartDate)
      const data = await getPlanMatrix({
        start_date: nextStartDate,
        days,
        page: nextPage,
        page_size: pageSize,
        sort_by: 'item_name',
        sort_dir: 'asc',
      })
      const nextWeeks = buildWeeks(data.dates ?? [])
      setWeeks(nextWeeks)
      setRows((data.rows ?? []).map((row) => {
        const weekValues = Object.fromEntries(nextWeeks.map((week) => [
          week.key,
          week.dates.reduce((sum, date) => sum + Number(row.days?.[date] || 0), 0),
        ]))
        return { ...row, weekValues }
      }))
      setTotal(data.total ?? 0)
      setPage(nextPage)
      setPending([])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  function updateWeek(row: RowVm, week: WeekInfo, value: string) {
    const nextValue = Number(value || 0)
    const friday = week.friday
    setRows((current) => current.map((item) => {
      if (item.item_id !== row.item_id) return item
      const weekValues = { ...item.weekValues, [week.key]: nextValue }
      const month_plan = Object.values(weekValues).reduce((sum, v) => sum + Number(v || 0), 0)
      return { ...item, weekValues, month_plan }
    }))
    setPending((current) => {
      const rest = current.filter((entry) => !(entry.item_id === row.item_id && entry.date === friday))
      return [...rest, { item_id: row.item_id, date: friday, qty: nextValue }]
    })
  }

  async function save() {
    if (!pending.length) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const result = await bulkUpsertPlan(pending)
      setMessage(`Сохранено записей плана: ${result.saved}`)
      await load(page, startDate)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function searchItems() {
    if (searchQuery.trim().length < 2) {
      setSearchRows([])
      return
    }
    setSearching(true)
    setError('')
    try {
      const result = await searchNomenclature(searchQuery.trim())
      setSearchRows(result.items ?? [])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSearching(false)
    }
  }

  async function addItem(item: NomenclatureSearchItem) {
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await ensurePlanItem(item)
      setMessage(`Добавлено в план: ${item.item_name}`)
      setSearchQuery('')
      setSearchRows([])
      await load(1, startDate)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function removeRow(row: RowVm) {
    const ok = window.confirm(`Удалить строку плана: ${row.item_name}?`)
    if (!ok) return
    setDeletingId(row.item_id)
    setError('')
    setMessage('')
    try {
      await deletePlanRow({ item_id: row.item_id, start_date: startDate, days: rollingThreeMonths(startDate) })
      setMessage(`Строка удалена: ${row.item_name}`)
      await load(page, startDate)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeletingId(null)
    }
  }

  useEffect(() => {
    void load(1, initialStartDate.current)
  }, [load])

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование / План выпуска техники квартальный</div>
        <div className="runBadge">Строк: {total}</div>
      </div>

      <DocumentWindow
        title="План выпуска техники квартальный"
        subtitle="Скользящее окно на 3 месяца. Редактирование по недельным пятницам."
        hotkeys="Ctrl+S Сохранить · F5 Обновить"
        footer={(
          <StatusBar
            loading={loading || saving}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={pending.length}
            canPrev={page > 1}
            canNext={page < totalPages}
            onPrev={() => void load(Math.max(1, page - 1), startDate)}
            onNext={() => void load(Math.min(totalPages, page + 1), startDate)}
          />
        )}
      >
        <div className="commandBar">
          <button className="primary" onClick={() => void save()} disabled={!pending.length || loading || saving}>Сохранить изменения</button>
          <button onClick={() => void load(page, startDate)} disabled={loading || saving}>Обновить</button>
          <div className="barSeparator" />
          <label className="inlineControl">
            <span>Начало окна</span>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <button onClick={() => void load(1, startDate)} disabled={loading || saving}>Сформировать</button>
          <div className="barSeparator" />
          <span className="toolbarText">План на странице: {qty(quarterSum)}</span>
        </div>

        <div className="planSearchBar">
          <label>
            <span>Добавить номенклатуру в план</span>
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void searchItems()}
              placeholder="наименование / артикул / код"
            />
          </label>
          <button onClick={() => void searchItems()} disabled={searchQuery.trim().length < 2 || searching}>Найти</button>
          {searchRows.length > 0 && (
            <div className="planSearchResults">
              {searchRows.map((item) => (
                <button key={item.item_code} onClick={() => void addItem(item)}>
                  <strong>{item.item_name}</strong>
                  <span>{item.item_article || '—'} · {item.item_code}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="tablePane resultTablePane">
          <table className="journalTable quarterPlanTable">
            <thead>
              <tr>
                <th>#</th>
                <th></th>
                <th>Изделие</th>
                <th>План на окно</th>
                {weeks.map((week) => <th key={week.key}>{week.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={row.item_id}>
                  <td className="numCell"><strong>{(page - 1) * pageSize + index + 1}</strong></td>
                  <td className="rowActionCell">
                    <button onClick={() => void removeRow(row)} disabled={deletingId === row.item_id}>Удалить</button>
                  </td>
                  <td className="itemCell">
                    <strong>{row.item_name}</strong>
                    <span>{row.item_article || '—'} · {row.item_code}</span>
                  </td>
                  <td className="numCell"><strong>{qty(row.month_plan)}</strong></td>
                  {weeks.map((week) => (
                    <td key={week.key} className="weekPlanCell">
                      <input
                        type="number"
                        min="0"
                        step="1"
                        value={row.weekValues[week.key] ?? 0}
                        onChange={(e) => updateWeek(row, week, e.target.value)}
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
