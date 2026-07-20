import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type {
  DbrBoard,
  DbrBoardSlot,
  DbrProgram,
  DbrReleaseDayResult,
  DbrReleaseResult,
} from '../../domain/dbr'
import { dateRu, isoToday, qty, shiftIsoDate } from '../../lib/format'
import {
  activateDbrDrum,
  buildDbrDrum,
  getDbrBoard,
  listDbrPrograms,
  moveDbrSlot,
  refreshDbrGate,
  releaseDbrDay,
  releaseDbrSlot,
  rollForwardDbrDrum,
} from '../../services/dbr'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { DbrConfirmDialog } from '../dbr/DbrConfirmDialog'
import { DbrNav } from '../dbr/DbrNav'
import {
  dayLabel,
  drumSlotReleaseState,
  drumSlotShortageTitle,
  groupDrumSlotsByCell,
  indexDrumSlotsById,
  isWeekend,
  KIT_CLASS,
  releaseResultText,
} from './dbr-drum-board/model'

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

  // Two-step release of a single slot: dry-run preview → confirmed write.
  const [releaseFlow, setReleaseFlow] = useState<{
    slot: DbrBoardSlot
    preview: DbrReleaseResult
    result?: DbrReleaseResult
  } | null>(null)
  const [releaseBusy, setReleaseBusy] = useState(false)
  const [releaseError, setReleaseError] = useState('')

  // Two-step batch release of one day: pick day → dry-run summary → confirm.
  const [dayModal, setDayModal] = useState<{
    phase: 'pick' | 'preview' | 'done'
    day: string
    preview?: DbrReleaseDayResult
    result?: DbrReleaseDayResult
  } | null>(null)
  const [dayBusy, setDayBusy] = useState(false)
  const [dayError, setDayError] = useState('')

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
    return groupDrumSlotsByCell(board?.slots ?? [])
  }, [board])

  const slotById = useMemo(() => {
    return indexDrumSlotsById(board?.slots ?? [])
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

  // Step 1: dry-run preview of a single slot release, opens the confirm dialog.
  async function startRelease() {
    if (!selectedSlot) return
    setSaving(true)
    setError('')
    setMessage('')
    setReleaseError('')
    try {
      const preview = await releaseDbrSlot(selectedSlot.id, true)
      setReleaseFlow({ slot: selectedSlot, preview })
    } catch (e) {
      // 409 (non-green / not pending) surfaces here as a human message.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // Step 2: confirmed write to live 1С.
  async function confirmRelease() {
    if (!releaseFlow) return
    setReleaseBusy(true)
    setReleaseError('')
    try {
      const result = await releaseDbrSlot(releaseFlow.slot.id, false)
      setReleaseFlow((prev) => (prev ? { ...prev, result } : prev))
      await load()
    } catch (e) {
      setReleaseError(e instanceof Error ? e.message : String(e))
    } finally {
      setReleaseBusy(false)
    }
  }

  function closeReleaseFlow() {
    const wasDone = Boolean(releaseFlow?.result)
    setReleaseFlow(null)
    if (wasDone) setSelectedSlot(null)
  }

  // Batch release of one day — step 1: dry-run summary of every green+pending slot.
  async function runDayPreview(day: string) {
    if (!schedule) return
    setDayBusy(true)
    setDayError('')
    try {
      const preview = await releaseDbrDay(schedule.id, day, true)
      setDayModal({ phase: 'preview', day, preview })
    } catch (e) {
      setDayError(e instanceof Error ? e.message : String(e))
    } finally {
      setDayBusy(false)
    }
  }

  // Batch release — step 2: confirmed write, per-slot report.
  async function confirmDay() {
    if (!schedule || !dayModal) return
    setDayBusy(true)
    setDayError('')
    try {
      const result = await releaseDbrDay(schedule.id, dayModal.day, false)
      setDayModal((prev) => (prev ? { ...prev, phase: 'done', result } : prev))
      await load()
    } catch (e) {
      setDayError(e instanceof Error ? e.message : String(e))
    } finally {
      setDayBusy(false)
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
          <button
            onClick={() => { setDayError(''); setDayModal({ phase: 'pick', day: isoToday() }) }}
            disabled={saving || !schedule}
          >
            Релиз дня…
          </button>
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
                              const shortageText = drumSlotShortageTitle(slot)
                              const released = drumSlotReleaseState(slot).alreadyReleased
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
                                  {released && (
                                    <span className="dbrSlotOrderBadge" title="Заказ создан в 1С">
                                      ✓ заказ 1С{slot.one_c_order_number ? ` № ${slot.one_c_order_number}` : ''}
                                    </span>
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
      {selectedSlot && !releaseFlow && (
        <div
          className="dialogOverlay"
          role="dialog"
          aria-modal="true"
          aria-label={`Плитка: ${selectedSlot.item_name || selectedSlot.item_code}`}
          onClick={() => setSelectedSlot(null)}
        >
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
                  <label htmlFor="dbr-move-date">Перенести на дату</label>
                  <input
                    id="dbr-move-date"
                    type="date"
                    value={moveDate}
                    onChange={(e) => setMoveDate(e.target.value)}
                    autoFocus
                  />
                </div>
                <div className="dialogField">
                  <label htmlFor="dbr-move-resource">Участок</label>
                  <select
                    id="dbr-move-resource"
                    value={moveResource}
                    onChange={(e) => setMoveResource(e.target.value)}
                  >
                    {resources.map((r) => (
                      <option key={r.id} value={r.id}>{r.name || `Участок ${r.id}`}</option>
                    ))}
                  </select>
                </div>
              </div>

              {(() => {
                const { alreadyReleased, canRelease } = drumSlotReleaseState(selectedSlot)
                return (
                  <div className="fieldHint">
                    {alreadyReleased
                      ? 'Плитка уже релизнута — заказ в 1С создан.'
                      : canRelease
                        ? 'Релиз создаёт заказ на производство в живой 1С. Сначала откроется предпросмотр документа.'
                        : 'Релиз доступен только для зелёных (green) плиток в статусе «pending». Обновите гейт комплектности.'}
                  </div>
                )
              })()}
            </div>
            <div className="dialogFooter">
              <button onClick={() => setSelectedSlot(null)}>Закрыть</button>
              <button
                onClick={() => void startRelease()}
                disabled={
                  saving ||
                  selectedSlot.kit_status !== 'green' ||
                  (selectedSlot.release_status || 'pending') !== 'pending'
                }
              >
                Релиз…
              </button>
              <button className="primary" onClick={() => void doMove()} disabled={saving}>Перенести</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Build dialog ─────────────────────────────────────────────── */}
      {buildOpen && (
        <div
          className="dialogOverlay"
          role="dialog"
          aria-modal="true"
          aria-label="Построить график из программы"
          onClick={() => setBuildOpen(false)}
        >
          <div className="dialogBox" onClick={(e) => e.stopPropagation()}>
            <div className="dialogHeader">Построить график из программы</div>
            <div className="dialogBody">
              {approvedPrograms.length ? (
                <div className="dialogField">
                  <label htmlFor="dbr-build-program">Утверждённая программа</label>
                  <select
                    id="dbr-build-program"
                    value={buildProgramId}
                    onChange={(e) => setBuildProgramId(e.target.value)}
                    autoFocus
                  >
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

      {/* ── Single-slot release: preview → confirm → 1С order number ──── */}
      {releaseFlow && (
        <DbrConfirmDialog
          title={`Релиз плитки — ${releaseFlow.slot.item_name || releaseFlow.slot.item_code}`}
          phase={releaseFlow.result ? 'done' : 'preview'}
          busy={releaseBusy}
          error={releaseError}
          onClose={closeReleaseFlow}
          onConfirm={() => void confirmRelease()}
        >
          {(() => {
            const { slot, preview, result } = releaseFlow
            return (
              <>
                <div className="dbrDocSummary">
                  <div><span>Документ</span><strong>Заказ на производство · № {preview.number}</strong></div>
                  <div><span>Изделие</span><strong>{slot.item_code} — {slot.item_name}</strong></div>
                  <div><span>Количество</span><strong>{qty(slot.qty)} шт</strong></div>
                  <div><span>Участок</span><strong>{slot.resource_name || `#${slot.resource_id}`}</strong></div>
                  <div><span>Дата запуска / выпуска</span><strong>{dateRu(slot.date)}</strong></div>
                </div>
                {result && (
                  <div className="dbrResultBox">
                    {result.created ? (
                      <p>Заказ создан в 1С: <strong>№ {result.number}</strong>{result.one_c_order_ref ? <span className="dbrRefKey"> · ref {result.one_c_order_ref}</span> : null}</p>
                    ) : result.already_released ? (
                      <p>Заказ уже был создан ранее: <strong>№ {result.number}</strong>{result.one_c_order_ref ? <span className="dbrRefKey"> · ref {result.one_c_order_ref}</span> : null}</p>
                    ) : result.error ? (
                      <p className="dbrResultError">Ошибка записи в 1С: {result.error}</p>
                    ) : (
                      <p>{result.note}</p>
                    )}
                  </div>
                )}
                {preview.payload && (
                  <details className="dbrPayloadDetails">
                    <summary>Показать payload документа 1С</summary>
                    <pre className="dialogPreview">{JSON.stringify(preview.payload, null, 2)}</pre>
                  </details>
                )}
              </>
            )
          })()}
        </DbrConfirmDialog>
      )}

      {/* ── Batch release of one day: pick → dry-run summary → confirm ── */}
      {dayModal && (
        <div
          className="dialogOverlay"
          role="dialog"
          aria-modal="true"
          aria-label="Релиз дня"
          onClick={dayBusy ? undefined : () => setDayModal(null)}
        >
          <div className="dialogBox dbrConfirmBox dbrDayBox" onClick={(e) => e.stopPropagation()}>
            <div className={`dialogHeader${dayModal.phase === 'done' ? ' dbrDoneHeader' : ''}`}>
              Релиз дня — {dateRu(dayModal.day)}
            </div>
            <div className="dialogBody">
              {dayModal.phase === 'preview' && (
                <div className="dbrLiveWarn">
                  ⚠ Будет создан заказ в живой 1С по каждому зелёному слоту этого дня.
                </div>
              )}
              {dayModal.phase === 'done' && (
                <div className="dbrDoneBanner">✓ Проведено в живой 1С. Это уже не предпросмотр.</div>
              )}
              {dayError && <div className="dialogError">{dayError}</div>}

              {dayModal.phase === 'pick' && (
                <div className="dialogField">
                  <label htmlFor="dbr-release-day">День для релиза</label>
                  <input
                    id="dbr-release-day"
                    type="date"
                    value={dayModal.day}
                    onChange={(e) => setDayModal({ phase: 'pick', day: e.target.value })}
                    autoFocus
                  />
                  <div className="fieldHint">
                    Будут релизнуты все зелёные плитки этого дня по активному графику №{schedule?.id}.
                  </div>
                </div>
              )}

              {(dayModal.phase === 'preview' || dayModal.phase === 'done') && (() => {
                const report = dayModal.result ?? dayModal.preview
                if (!report) return null
                return (
                  <>
                    <div className="dbrDaySummaryLine">
                      Слотов: {report.slots_total} ·{' '}
                      {dayModal.phase === 'done'
                        ? `создано заказов: ${report.released}`
                        : `к релизу: ${report.previews}`}{' '}
                      · отказов/ошибок: {report.errors}
                    </div>
                    <div className="dbrFeederTableWrap">
                      <table className="journalTable dbrTable">
                        <thead>
                          <tr><th>Слот</th><th>Изделие</th><th className="numCell">Кол-во</th><th>Результат</th></tr>
                        </thead>
                        <tbody>
                          {!report.results.length && (
                            <tr><td colSpan={4} className="emptyCell">Нет зелёных плиток к релизу в этот день.</td></tr>
                          )}
                          {report.results.map((r) => {
                            const s = slotById.get(r.slot_id)
                            const fail = Boolean(r.conflict || r.error)
                            const text = releaseResultText(r, dayModal.phase === 'done')
                            return (
                              <tr key={r.slot_id} className={fail ? 'dbrGapRow' : undefined}>
                                <td>№{r.slot_id}</td>
                                <td>{s ? `${s.item_code} — ${s.item_name}` : '—'}</td>
                                <td className="numCell">{s ? qty(s.qty) : '—'}</td>
                                <td>{text}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  </>
                )
              })()}
            </div>
            <div className="dialogFooter">
              <button onClick={() => setDayModal(null)} disabled={dayBusy}>
                {dayModal.phase === 'done' ? 'Закрыть' : 'Отмена'}
              </button>
              {dayModal.phase === 'pick' && (
                <button className="primary" onClick={() => void runDayPreview(dayModal.day)} disabled={dayBusy}>
                  {dayBusy ? 'Загрузка…' : 'Предпросмотр'}
                </button>
              )}
              {dayModal.phase === 'preview' && (
                <button
                  className="dbrDanger"
                  onClick={() => void confirmDay()}
                  disabled={dayBusy || !dayModal.preview?.slots_total}
                >
                  {dayBusy ? 'Отправка…' : 'Провести в 1С'}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
