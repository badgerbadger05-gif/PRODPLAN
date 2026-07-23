import { dateRu, isoToday, qty } from '../../lib/format'
import { DbrNav } from '../dbr/DbrNav'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { dayLabel, isWeekend, KIT_CLASS } from './dbr-drum-board/model'
import { useDbrDrumBoardController } from './dbr-drum-board/useDbrDrumBoardController'

const shown = (value: number | null | undefined) => value == null ? '—' : qty(value)

export function DbrDrumBoardPage() {
  const { board, loading, error, selectedSlot, setSelectedSlot, load, schedule, slotsByCell } = useDbrDrumBoardController()
  const today = isoToday()
  const days = board?.days ?? []
  const resources = board?.resources ?? []
  const kpi = board?.kpi
  const meta = board?.meta
  const readOnly = Boolean(meta?.read_only)

  return <main className="workArea">
    <div className="topLine"><div className="breadcrumbs">Планирование DBR / Барабан сборки</div><div className="runBadge">{schedule ? `График №${schedule.id} · ${schedule.status}` : 'Нет сохранённого графика'}</div></div>
    <DocumentWindow title="Барабан сборки" subtitle="Сохранённый Ledger-снимок: дни × участки и плановые плитки выпуска" hotkeys="F5 Обновить" footer={<StatusBar loading={loading} visibleFrom={board?.slots.length ? 1 : 0} visibleTo={board?.slots.length ?? 0} total={board?.slots.length ?? 0} selectedCount={selectedSlot ? 1 : 0} canPrev={false} canNext={false} onPrev={() => undefined} onNext={() => undefined} />}>
      <DbrNav />
      <div className="commandBar dbrBoardBar"><button onClick={() => void load()} disabled={loading}>Обновить снимок</button><div className="commandBarSpacer" /></div>
      {loading && <div className="srOnly" role="status">Загрузка сохранённого барабана…</div>}
      {error && <div className="errorLine" role="alert">{error}</div>}
      <div className="dbrFeederNotice">Только чтение: барабан показывает сохранённый снимок принятого Ledger-поколения. Гейт комплектности, факт выпуска и команды 1С здесь недоступны.</div>
      {board && <div className="dbrFeederNotice" data-testid="drum-snapshot-lineage">Снимок #{meta?.snapshot_id ?? '—'} · Ledger-поколение #{meta?.ledger_generation ?? '—'} · срез {dateRu(meta?.cutoff) || '—'} · MRP: {meta?.runs?.length ? meta.runs.map((run) => `run #${run.run_id} · freeze ${run.freeze_version}`).join('; ') : '—'}</div>}
      {board?.calendar_fallback && <div className="warningLine">Календарь работ не покрывает весь горизонт графика. Для непокрытых дат использован график пн–пт.</div>}
      {board && <div className="dbrKpis">
        <div className="dbrKpi"><div className="dbrKpiLabel">Комплектация</div><div className="dbrKpiValue"><span className="dbrDot g" />{shown(kpi?.green)}<span className="dbrDot y" />{shown(kpi?.yellow)}<span className="dbrDot r" />{shown(kpi?.red)}<span className="dbrDot n" />{shown(kpi?.unknown)}</div><div className="dbrKpiSub">гейт комплектности: {kpi?.kit_gate_status ?? 'unavailable'}</div></div>
        <div className="dbrKpi"><div className="dbrKpiLabel">План / факт</div><div className="dbrKpiValue">{shown(kpi?.fact_qty)} / {shown(kpi?.plan_qty)}</div><div className="dbrKpiSub">факт выпуска: {kpi?.execution_status ?? 'unavailable'} · слотов: {shown(kpi?.slots)}</div></div>
        <div className={`dbrKpi ${board?.gaps.length ? 'alert' : ''}`}><div className="dbrKpiLabel">Разрывы мощности</div><div className="dbrKpiValue">{board?.gaps.length ? board.gaps.length : 'нет'}</div><div className="dbrKpiSub">{board?.gaps.length ? 'см. таблицу ниже' : 'все дни в такте'}</div></div>
      </div>}
      {!board && !loading ? <div className="dbrEmpty"><div className="dbrEmptyTitle">Сохранённый барабан недоступен</div><div>Дождитесь принятого Ledger-снимка и фоновой публикации барабана.</div></div> : !schedule ? <div className="dbrEmpty"><div className="dbrEmptyTitle">Активного графика в снимке нет</div><div>Постройте график в отдельном контуре, затем дождитесь его публикации в следующем снимке.</div></div> : !days.length || !resources.length ? <div className="dbrEmpty"><div className="dbrEmptyTitle">В сохранённом снимке нет данных</div></div> : <div className="dbrGridWrap"><table className="dbrGrid"><thead><tr><th className="dbrWsCol">Участок</th>{days.map((day) => <th key={day} className={`dbrDayCol${isWeekend(day) ? ' weekend' : ''}${day === today ? ' today' : ''}`}>{dayLabel(day)}</th>)}</tr></thead><tbody>{resources.map((resource) => <tr key={resource.id}><td className="dbrWsCol"><div className="dbrWsName">{resource.name || `Участок ${resource.id}`}</div></td>{days.map((day) => <td key={day} className={`dbrDayCol${isWeekend(day) ? ' weekend' : ''}${day === today ? ' today' : ''}`}><div className="dbrCell">{(slotsByCell.get(`${resource.id}::${day}`) ?? []).map((slot) => <button type="button" key={slot.id} className={`dbrSlot ${KIT_CLASS[slot.kit_status] ?? 'kitUnknown'}${selectedSlot?.id === slot.id ? ' active' : ''}`} title={slot.item_name || slot.item_code || ''} onClick={() => setSelectedSlot(slot)}><span className="dbrSlotQty">{shown(slot.produced_qty)}/{qty(slot.qty)}</span><span className="dbrSlotName">{slot.item_name || slot.item_code || `#${slot.item_id}`}</span>{slot.item_code && slot.item_code !== slot.item_name && <span className="dbrSlotCode">{slot.item_code}</span>}</button>)}</div></td>)}</tr>)}</tbody></table></div>}
      {schedule && !!board?.gaps.length && <section className="dbrSection dbrGaps"><h2>Разрывы мощности</h2><table className="journalTable dbrTable"><thead><tr><th>Дата</th><th>Участок</th><th>Изделие</th><th className="numCell">Требуется</th><th className="numCell">Такт</th><th className="numCell">Дефицит</th></tr></thead><tbody>{board.gaps.map((gap) => <tr key={gap.id} className="dbrGapRow"><td>{dateRu(gap.date) || '—'}</td><td>{gap.resource_name || '—'}</td><td>{gap.item_name || gap.item_code || '—'}</td><td className="numCell">{qty(gap.required_qty)}</td><td className="numCell">{qty(gap.takt_qty)}</td><td className="numCell"><strong>{qty(gap.gap_qty)}</strong></td></tr>)}</tbody></table></section>}
    </DocumentWindow>
    {selectedSlot && <div className="dialogOverlay" role="dialog" aria-modal="true" aria-label={`Плитка: ${selectedSlot.item_name || selectedSlot.item_code}`} onClick={() => setSelectedSlot(null)}><div className="dialogBox" onClick={(e) => e.stopPropagation()}><div className="dialogHeader">Плитка: {selectedSlot.item_name || selectedSlot.item_code}</div><div className="dialogBody"><div className="dbrSlotDetails"><div><b>Дата:</b> {dateRu(selectedSlot.date)}</div><div><b>Участок:</b> {selectedSlot.resource_name || selectedSlot.resource_id}</div><div><b>Номенклатура:</b> {selectedSlot.item_code} — {selectedSlot.item_name}</div><div><b>Кол-во (факт/план):</b> {shown(selectedSlot.produced_qty)} / {qty(selectedSlot.qty)}</div><div><b>Комплектность:</b> {selectedSlot.kit_gate_status ?? 'unavailable'}</div><div><b>Исполнение:</b> {selectedSlot.execution_status ?? 'unavailable'}</div></div></div><div className="dialogFooter"><button onClick={() => setSelectedSlot(null)}>Закрыть</button>{!readOnly && null}</div></div></div>}
  </main>
}
