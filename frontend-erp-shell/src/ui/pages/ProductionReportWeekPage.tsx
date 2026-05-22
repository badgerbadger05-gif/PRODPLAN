import { useEffect, useMemo, useState } from 'react'
import type { ProductionReportFactEntry, ProductionReportWeekDay, ProductionReportWeekRow } from '../../domain/productionReport'
import { dateRu, isoToday, qty, shiftIsoDate } from '../../lib/format'
import { bulkUpsertProductionReportFact, closeProductionReportDay, getProductionReportWeek } from '../../services/productionReport'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

type RowVm = ProductionReportWeekRow & {
  factInput: Record<string, number>
}

function dayLabel(date: string) {
  const labels = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб']
  const d = new Date(`${date}T00:00:00`)
  return `${labels[d.getDay()]} ${date.slice(8, 10)}.${date.slice(5, 7)}`
}

function isClosed(day?: ProductionReportWeekDay) {
  return String(day?.close_status || '').toUpperCase() === 'CLOSED'
}

export function ProductionReportWeekPage() {
  const [anyDate, setAnyDate] = useState(isoToday())
  const [closeDate, setCloseDate] = useState('')
  const [weekStart, setWeekStart] = useState('')
  const [days, setDays] = useState<ProductionReportWeekDay[]>([])
  const [rows, setRows] = useState<RowVm[]>([])
  const [closeHint, setCloseHint] = useState<{ today: string; close_date: string; target_date: string } | null>(null)
  const [pending, setPending] = useState<ProductionReportFactEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [closing, setClosing] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const dayByDate = useMemo(() => Object.fromEntries(days.map((day) => [day.date, day])), [days])
  const closeDateInWeek = !closeDate || days.some((day) => day.date === closeDate)
  const canCloseDay = Boolean(closeDate && closeDateInWeek && !loading && !saving && !closing)

  async function load(nextDate = anyDate) {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await getProductionReportWeek({ any_date_in_week: nextDate })
      setWeekStart(data.week_start)
      setDays(data.days ?? [])
      setCloseHint(data.close_hint ?? null)
      setCloseDate(data.close_hint?.close_date ?? data.days?.[0]?.date ?? '')
      setRows((data.rows ?? []).map((row) => ({
        ...row,
        factInput: Object.fromEntries((data.days ?? []).map((day) => [day.date, Number(row.fact_by_day?.[day.date] || 0)])),
      })))
      setPending([])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function updateFact(row: RowVm, date: string, value: string) {
    const fact = Number(value || 0)
    setRows((current) => current.map((item) => (
      item.item_id === row.item_id
        ? { ...item, factInput: { ...item.factInput, [date]: fact } }
        : item
    )))
    setPending((current) => {
      const rest = current.filter((entry) => !(entry.item_id === row.item_id && entry.date === date))
      return [...rest, { item_id: row.item_id, date, fact_qty: fact }]
    })
  }

  async function saveFacts() {
    if (!pending.length) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const result = await bulkUpsertProductionReportFact({ entries: pending, rerun_editable_date: closeDate || undefined })
      setMessage(`Сохранено строк факта: ${result.saved}`)
      await load(anyDate)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function closeDay() {
    if (!canCloseDay) return
    setClosing(true)
    setError('')
    setMessage('')
    try {
      await closeProductionReportDay({ close_date: closeDate, closed_by: 'erp-shell' })
      setMessage(`День закрыт: ${closeDate}`)
      await load(anyDate)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setClosing(false)
    }
  }

  function goWeek(delta: number) {
    const nextDate = shiftIsoDate(weekStart || anyDate, delta)
    setAnyDate(nextDate)
    void load(nextDate)
  }

  useEffect(() => {
    void load(anyDate)
  }, [])

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Отчёт о выпуске техники недельный</div>
        <div className="runBadge">Неделя: {dateRu(weekStart) || '—'}</div>
      </div>

      <DocumentWindow
        title="Отчёт о выпуске техники недельный"
        subtitle="Неделя Пн-Вс. Факт за закрытый день только для чтения."
        hotkeys="F5 Обновить · Ctrl+S Сохранить"
        footer={(
          <StatusBar
            loading={loading || saving || closing}
            visibleFrom={rows.length ? 1 : 0}
            visibleTo={rows.length}
            total={rows.length}
            selectedCount={pending.length}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => goWeek(-7)} disabled={loading || saving || closing}>Пред. неделя</button>
          <button onClick={() => goWeek(7)} disabled={loading || saving || closing}>След. неделя</button>
          <button onClick={() => void load(anyDate)} disabled={loading || saving || closing}>Загрузить</button>
          <div className="barSeparator" />
          <label className="inlineControl">
            <span>Дата недели</span>
            <input type="date" value={anyDate} onChange={(e) => setAnyDate(e.target.value)} />
          </label>
          <label className="inlineControl">
            <span>Закрыть дату</span>
            <input type="date" value={closeDate} onChange={(e) => setCloseDate(e.target.value)} />
          </label>
          <div className="barSeparator" />
          <button className="primary" onClick={() => void saveFacts()} disabled={!pending.length || loading || saving || closing}>Сохранить факт</button>
          <button onClick={() => void closeDay()} disabled={!canCloseDay}>Закрыть день</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}
        {closeHint && (
          <div className="hintLine">
            <strong>Закрываемый день:</strong> {dateRu(closeHint.close_date)}
            <span> перенос на </span>
            <strong>{dateRu(closeHint.target_date)}</strong>
            <span> · повторное закрытие пересчитает перенос</span>
          </div>
        )}
        {!closeDateInWeek && <div className="errorLine">Закрываемая дата должна быть внутри текущей недели отчёта</div>}

        <div className="tablePane resultTablePane">
          <table className="journalTable weeklyReportTable">
            <thead>
              <tr>
                <th>Изделие</th>
                {days.map((day) => (
                  <th key={day.date} className={isClosed(day) ? 'closedDay' : ''}>
                    <div>{dayLabel(day.date)}</div>
                    {isClosed(day) && <span>закрыто: план {qty(day.closed_planned)}, факт {qty(day.closed_fact)}, перенос {qty(day.carry_qty)}</span>}
                  </th>
                ))}
                <th>План</th>
                <th>Факт</th>
                <th>Остаток</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.item_id}>
                  <td className="itemCell">
                    <strong>{row.item_name}</strong>
                    <span>{row.item_article || '—'} · {row.item_code}</span>
                  </td>
                  {days.map((day) => {
                    const closed = isClosed(dayByDate[day.date])
                    const plan = Number(row.plan_by_day?.[day.date] || 0)
                    const carry = Number(row.carry_by_day?.[day.date] || 0)
                    return (
                      <td key={day.date} className={`weekDayCell ${closed ? 'closedDay' : ''}`}>
                        <span>план: {qty(plan)}</span>
                        {carry > 0 && <span className="carryText">перенос: {qty(carry)}</span>}
                        <input
                          type="number"
                          min="0"
                          step="1"
                          value={row.factInput[day.date] ?? 0}
                          disabled={closed}
                          onChange={(e) => updateFact(row, day.date, e.target.value)}
                        />
                      </td>
                    )
                  })}
                  <td className="numCell"><strong>{qty(row.plan_week)}</strong></td>
                  <td className="numCell"><strong>{qty(row.fact_week)}</strong></td>
                  <td className="numCell"><strong>{qty(row.remaining_week)}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
