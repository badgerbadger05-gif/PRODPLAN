import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { DbrBoard, DbrBoardSlot, DbrProgram } from '../../domain/dbr'
import { dateRu, isoToday, qty, shiftIsoDate } from '../../lib/format'
import {
  activateDbrDrum,
  buildDbrDrum,
  getDbrBoard,
  listDbrPrograms,
  moveDbrSlot,
  refreshDbrGate,
  releaseDbrSlot,
  rollForwardDbrDrum,
} from '../../services/dbr'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { DbrNav } from '../dbr/DbrNav'

const KIT_CLASS: Record<string, string> = {
  green: 'kitGreen',
  yellow: 'kitYellow',
  red: 'kitRed',
  unknown: 'kitUnknown',
}

function isWeekend(iso: string) {
  const day = new Date(`${iso}T00:00:00`).getDay()
  return day === 0 || day === 6
}

function dayLabel(iso: string) {
  const date = new Date(`${iso}T00:00:00`)
  const weekday = date.toLocaleDateString('ru-RU', { weekday: 'short' })
  const parts = iso.split('-')
  return `${parts[2]}.${parts[1]} ${weekday}`
}

export function DbrDrumBoardPage() {
  const [board, setBoard] = useState<DbrBoard | null>(null)
  const [dateFrom, setDateFrom] = useState(shiftIsoDate(isoToday(), -2))
  const [dateTo, setDateTo] = useState(shiftIsoDate(isoToday(), 14))
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const [selectedSlot, setSelectedSlot] = useState<DbrBoardSlot | null>(null)
  const [moveDate, setMoveDate] = useState('')
  const [moveResource, setMoveResource] = useState<string>('')

  const [buildOpen, setBuildOpen] = useState(false)
  const [approvedPrograms, setApprovedPrograms] = useState<DbrProgram[]>([])
  const [buildProgramId, setBuildProgramId] = useState<string>('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setBoard(await getDbrBoard({ date_from: dateFrom, date_to: dateTo }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [dateFrom, dateTo])

  useEffect(() => {
    void load()
  }, [load])

  const schedule = board?.schedule ?? null
  const today = isoToday()

  const slotsByCell = useMemo(() => {
    const map = new Map<string, DbrBoardSlot[]>()
    for (const slot of board?.slots ?? []) {
      const key = `${slot.resource_id}::${slot.date}`
      const bucket = map.get(key)
      if (bucket) bucket.push(slot)
      else map.set(key, [slot])
    }
    return map
  }, [board])

  function openSlot(slot: DbrBoardSlot) {
    setSelectedSlot(slot)
    setMoveDate(slot.date)
    setMoveResource(String(slot.resource_id))
    setMessage('')
    setError('')
  }

  async function refreshGate() {
    if (!schedule) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const res = await refreshDbrGate(schedule.id)
      setMessage(`Гейт обновлён: 🟢 ${res.green} · 🟡 ${res.yellow} · 🔴 ${res.red} (изменено ${res.updated})`)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function rollForward() {
    if (!schedule) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const res = await rollForwardDbrDrum(schedule.id)
      if (res.horizon_exhausted) {
        setMessage('Горизонт графика исчерпан — постройте новый период')
      } else {
        let msg = `Перенесено плиток: ${res.moved} · закрыто выпуском: ${res.closed}`
        if (res.overloaded) msg += ` · с перегрузом: ${res.overloaded}`
        setMessage(msg)
      }
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function openBuild() {
    setBuildOpen(true)
    setError('')
    try {
      const list = await listDbrPrograms('approved')
      setApprovedPrograms(list)
      setBuildProgramId(list.length ? String(list[0].id) : '')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function runBuild() {
    const programId = Number(buildProgramId)
    if (!programId) {
      setError('Выберите утверждённую программу')
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const built = await buildDbrDrum(programId)
      await activateDbrDrum(built.schedule.id)
      const carried = built.carried_over?.length ?? 0
      setMessage(
        `График №${built.schedule.id} построен и активирован` +
          (carried ? ` · перенесено на след. период: ${carried}` : ''),
      )
      setBuildOpen(false)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function doMove() {
    if (!selectedSlot) return
    const resourceId = Number(moveResource)
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const res = await moveDbrSlot(
        selectedSlot.id,
        moveDate,
        resourceId !== selectedSlot.resource_id ? resourceId : undefined,
      )
      setMessage(res.moved ? `Плитка перенесена на ${dateRu(res.to)}` : 'Плитка осталась на месте')
      setSelectedSlot(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function doRelease() {
    if (!selectedSlot) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const res = await releaseDbrSlot(selectedSlot.id)
      setMessage(
        res.stub
          ? 'Тестовый режим: статус изменён на «релиз», в 1С не пишется'
          : `Плитка релизнута (${res.release_status})`,
      )
      setSelectedSlot(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const kpi = board?.kpi
  const days = board?.days ?? []
  const resources = board?.resources ?? []

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование DBR / Барабан сборки</div>
        <div className="runBadge">
          {schedule ? `График №${schedule.id} · ${schedule.status}` : 'Нет активного графика'}
        </div>
      </div>

      <DocumentWindow
        title="Барабан сборки"
        subtitle="Дни × участки: плитки выпуска с гейтом комплектности и разрывами мощности"
        hotkeys="F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={board?.slots.length ? 1 : 0}
            visibleTo={board?.slots.length ?? 0}
            total={board?.slots.length ?? 0}
            selectedCount={selectedSlot ? 1 : 0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <DbrNav />

        <div className="commandBar dbrBoardBar">
          <label className="inlineControl">
            <span>С</span>
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
          </label>
          <label className="inlineControl">
            <span>По</span>
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
          </label>
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
          <div className="barSeparator" />
          <button onClick={() => void refreshGate()} disabled={saving || !schedule}>Обновить гейт</button>
          <button onClick={() => void rollForward()} disabled={saving || !schedule}>Перенести невыполненное</button>
          <div className="commandBarSpacer" />
          <button className="primary" onClick={() => void openBuild()} disabled={saving}>Построить из программы…</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}
        {board?.calendar_fallback && (
          <div className="warningLine">
            Календарь работ не покрывает весь горизонт графика. Для непокрытых дат использован график пн–пт.
          </div>
        )}

        {/* ── KPI header ─────────────────────────────────────────────── */}
        <div className="dbrKpis">
          <div className="dbrKpi">
            <div className="dbrKpiLabel">Комплектация</div>
            <div className="dbrKpiValue">
              <span className="dbrDot g" />{kpi?.green ?? 0}
              <span className="dbrDot y" />{kpi?.yellow ?? 0}
              <span className="dbrDot r" />{kpi?.red ?? 0}
              <span className="dbrDot n" />{kpi?.unknown ?? 0}
            </div>
            <div className="dbrKpiSub">🟢 готово · 🟡 ждём приход · 🔴 дефицит · ⚪ не проверено</div>
          </div>
          <div className="dbrKpi">
            <div className="dbrKpiLabel">План / факт</div>
            <div className="dbrKpiValue">{qty(kpi?.fact_qty ?? 0)} / {qty(kpi?.plan_qty ?? 0)}</div>
            <div className="dbrKpiSub">шт · слотов: {kpi?.slots ?? 0}</div>
          </div>
          <div className={`dbrKpi ${board?.gaps.length ? 'alert' : ''}`}>
            <div className="dbrKpiLabel">Разрывы мощности</div>
            <div className="dbrKpiValue">{board?.gaps.length ? board.gaps.length : 'нет'}</div>
            <div className="dbrKpiSub">{board?.gaps.length ? 'см. таблицу ниже' : 'все дни в такте'}</div>
          </div>
        </div>

        {/* ── Board grid ─────────────────────────────────────────────── */}
        {!schedule ? (
          <div className="dbrEmpty">
            <div className="dbrEmptyTitle">Активного графика сборки нет</div>
            <div>Постройте график по утверждённой производственной программе, чтобы увидеть барабан.</div>
            <div className="dbrEmptyActions">
              <button className="primary" onClick={() => void openBuild()} disabled={saving}>Построить из программы…</button>
              <Link to="/dbr/programs" className="navItem">К программам</Link>
            </div>
          </div>
        ) : !days.length || !resources.length ? (
          <div className="dbrEmpty">
            <div className="dbrEmptyTitle">В выбранном диапазоне нет данных</div>
            <div>Измените диапазон дат или обновите гейт комплектности.</div>
          </div>
        ) : (
          <div className="dbrGridWrap">
            <table className="dbrGrid">
              <thead>
                <tr>
                  <th className="dbrWsCol">Участок</th>
                  {days.map((day) => (
                    <th
                      key={day}
                      className={`dbrDayCol${isWeekend(day) ? ' weekend' : ''}${day === today ? ' today' : ''}`}
                    >
                      {dayLabel(day)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {resources.map((resource) => (
                  <tr key={resource.id}>
                    <td className="dbrWsCol"><div className="dbrWsName">{resource.name || `Участок ${resource.id}`}</div></td>
                    {days.map((day) => {
                      const slots = slotsByCell.get(`${resource.id}::${day}`) ?? []
                      return (
                        <td key={day} className={`dbrDayCol${isWeekend(day) ? ' weekend' : ''}${day === today ? ' today' : ''}`}>
                          <div className="dbrCell">
                            {slots.map((slot) => {
                              const shortageText = (slot.shortage ?? [])
                                .map((s) => `${s.item}: нужно ${qty(s.required)}, есть ${qty(s.available)}${s.warehouse ? ` (${s.warehouse})` : ''}`)
                                .join('\n')
                              const released = slot.release_status === 'released' || slot.release_status === 'completed'
                              return (
                                <button
                                  type="button"
                                  key={slot.id}
                                  className={`dbrSlot ${KIT_CLASS[slot.kit_status] ?? 'kitUnknown'}${released ? ' released' : ''}${selectedSlot?.id === slot.id ? ' active' : ''}`}
                                  title={shortageText ? `${slot.item_name || slot.item_code}\n${shortageText}` : (slot.item_name || slot.item_code || '')}
                                  onClick={() => openSlot(slot)}
                                >
                                  <span className="dbrSlotQty">
                                    {qty(slot.produced_qty)}/{qty(slot.qty)}
                                  </span>
                                  <span className="dbrSlotName">{slot.item_name || slot.item_code || `#${slot.item_id}`}</span>
                                  {slot.item_code && slot.item_code !== slot.item_name && (
                                    <span className="dbrSlotCode">{slot.item_code}</span>
                                  )}
                                </button>
                              )
                            })}
                          </div>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* ── Capacity gaps ──────────────────────────────────────────── */}
        {schedule && !!board?.gaps.length && (
          <section className="dbrSection dbrGaps">
            <h2>Разрывы мощности</h2>
            <table className="journalTable dbrTable">
              <thead>
                <tr>
                  <th className="dateCol">Дата</th>
                  <th className="itemCell">Участок</th>
                  <th className="itemCell">Изделие</th>
                  <th className="numCell">Требуется</th>
                  <th className="numCell">Такт</th>
                  <th className="numCell">Дефицит</th>
                </tr>
              </thead>
              <tbody>
                {board.gaps.map((gap) => (
                  <tr key={gap.id} className="dbrGapRow">
                    <td className="dateCol">{gap.date ? dateRu(gap.date) : '—'}</td>
                    <td className="itemCell">{gap.resource_name || (gap.resource_id != null ? `Участок ${gap.resource_id}` : '—')}</td>
                    <td className="itemCell">{gap.item_name || gap.item_code || '—'}</td>
                    <td className="numCell">{qty(gap.required_qty)}</td>
                    <td className="numCell">{qty(gap.takt_qty)}</td>
                    <td className="numCell"><strong>{qty(gap.gap_qty)}</strong></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
      </DocumentWindow>

      {/* ── Slot detail panel ────────────────────────────────────────── */}
      {selectedSlot && (
        <div className="dialogOverlay" onClick={() => setSelectedSlot(null)}>
          <div className="dialogBox" onClick={(e) => e.stopPropagation()}>
            <div className="dialogHeader">Плитка: {selectedSlot.item_name || selectedSlot.item_code}</div>
            <div className="dialogBody">
              <div className="dbrSlotDetails">
                <div><b>Дата:</b> {dateRu(selectedSlot.date)}</div>
                <div><b>Участок:</b> {selectedSlot.resource_name || selectedSlot.resource_id}</div>
                <div><b>Номенклатура:</b> {selectedSlot.item_code} — {selectedSlot.item_name}</div>
                <div><b>Кол-во (факт/план):</b> {qty(selectedSlot.produced_qty)} / {qty(selectedSlot.qty)}</div>
                <div><b>Комплектность:</b> {selectedSlot.kit_status}</div>
                <div><b>Статус релиза:</b> {selectedSlot.release_status || 'pending'}</div>
              </div>

              {!!(selectedSlot.shortage ?? []).length && (
                <div>
                  <h5 className="dbrShortageTitle">Чего не хватает</h5>
                  <table className="journalTable dbrTable dbrShortageTable">
                    <thead>
                      <tr>
                        <th className="itemCell">Позиция</th>
                        <th className="numCell">Требуется</th>
                        <th className="numCell">Доступно</th>
                        <th className="itemCell">Склад</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(selectedSlot.shortage ?? []).map((s, i) => (
                        <tr key={`${s.item}-${i}`}>
                          <td className="itemCell">{s.item}</td>
                          <td className="numCell">{qty(s.required)}</td>
                          <td className="numCell">{qty(s.available)}</td>
                          <td className="itemCell">{s.warehouse || ''}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="dbrMoveRow">
                <div className="dialogField">
                  <label>Перенести на дату</label>
                  <input type="date" value={moveDate} onChange={(e) => setMoveDate(e.target.value)} />
                </div>
                <div className="dialogField">
                  <label>Участок</label>
                  <select value={moveResource} onChange={(e) => setMoveResource(e.target.value)}>
                    {resources.map((r) => (
                      <option key={r.id} value={r.id}>{r.name || `Участок ${r.id}`}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="fieldHint">Релиз в тестовом режиме: статус меняется, запись в 1С не выполняется.</div>
            </div>
            <div className="dialogFooter">
              <button onClick={() => setSelectedSlot(null)}>Закрыть</button>
              <button onClick={() => void doRelease()} disabled={saving}>Релиз (тест)</button>
              <button className="primary" onClick={() => void doMove()} disabled={saving}>Перенести</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Build dialog ─────────────────────────────────────────────── */}
      {buildOpen && (
        <div className="dialogOverlay" onClick={() => setBuildOpen(false)}>
          <div className="dialogBox" onClick={(e) => e.stopPropagation()}>
            <div className="dialogHeader">Построить график из программы</div>
            <div className="dialogBody">
              {approvedPrograms.length ? (
                <div className="dialogField">
                  <label>Утверждённая программа</label>
                  <select value={buildProgramId} onChange={(e) => setBuildProgramId(e.target.value)}>
                    {approvedPrograms.map((p) => (
                      <option key={p.id} value={p.id}>
                        №{p.id} · {p.title || 'без названия'} · {dateRu(p.from_date)}—{dateRu(p.to_date)} · {p.items.length} строк
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div className="fieldHint">
                  Нет утверждённых программ. Создайте и утвердите программу на странице{' '}
                  <Link to="/dbr/programs">Программы</Link>.
                </div>
              )}
              <div className="fieldHint">График будет построен и сразу активирован (текущий активный станет superseded).</div>
            </div>
            <div className="dialogFooter">
              <button onClick={() => setBuildOpen(false)}>Отмена</button>
              <button className="primary" onClick={() => void runBuild()} disabled={saving || !approvedPrograms.length}>
                Построить и активировать
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
