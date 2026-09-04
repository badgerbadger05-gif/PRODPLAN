import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { listDrumSchedule, moveDrumSlot, type DrumScheduleResponse } from '../../../services/productionControl'
import { AsyncState } from '../../layout/AsyncState'
import { OutputFactSummary } from './OutputFactSummary'

type DrumSlot = DrumScheduleResponse['slots'][number]
type DrumGap = DrumScheduleResponse['gaps'][number]
type DrumExcluded = DrumScheduleResponse['excluded'][number]
type DrumEntry = DrumSlot | DrumGap
type DrumCoverageSource = NonNullable<DrumSlot['blocking_manifest'][number]['coverage_sources']>[number]
type DrumAction = DrumSlot['action_manifest'][number]

const horizonLabels: Record<string, string> = {
  now: 'Сейчас',
  transfer: 'После адресного перемещения',
  kitting: 'После комплектовки',
  committed: 'После подтверждённых поступлений',
  launch: 'После запуска обеспечения',
}

const phaseLabels: Record<string, string> = {
  now: 'Можно собирать сейчас',
  transfer: 'После перемещения',
  kitting: 'После комплектовки',
  committed: 'После подтверждённого поступления',
  launch: 'После запуска обеспечения',
  blocked: 'Пока не собирается',
  unavailable: 'Недостаточно данных',
  mixed: 'Часть остатка заблокирована',
}

const phaseClasses: Record<string, string> = {
  now: 'gateReady',
  transfer: 'gateTransfer',
  kitting: 'gateKitting',
  committed: 'gateCommitted',
  launch: 'gateLaunch',
  blocked: 'gateBlocked',
  unavailable: 'gateUnavailable',
  mixed: 'gateBlocked',
}

const actionLabels: Record<string, string> = {
  transfer: 'Переместить',
  kitting: 'Скомплектовать',
  make: 'Запустить узел / ДСЕ',
  rework: 'Запустить переработку',
  buy: 'Закупить',
  committed_supply: 'Подтверждённое поступление',
}

const coverageKindLabels: Record<string, string> = {
  point_of_use: 'В точке потребления',
  custody: 'Передано участку',
  transit: 'В пути',
  wip_order: 'В производстве',
  supplier_order: 'Поставка',
  other_stock: 'На других складах',
}

const confidenceLabels: Record<string, string> = {
  physical: 'факт Ledger',
  custody: 'передано участку',
  committed: 'подтверждено документом',
  forecast: 'прогноз при запуске',
}

const blockerLabels: Record<string, string> = {
  SHORTAGE: 'Не хватает остатка',
  REPLENISHMENT_POLICY_MISSING: 'Не задан способ обеспечения',
  HORIZON_DOES_NOT_ALLOW_REPLENISHMENT: 'Обеспечение ещё не входит в этот горизонт',
  BOM_CYCLE: 'Цикл в спецификации',
  FROZEN_BOM_MISSING: 'Нет замороженной спецификации',
  REPLENISHMENT_MODE_UNAVAILABLE: 'Недоступный способ обеспечения',
  LEAD_TIME_MISSING: 'Не задан срок обеспечения',
  OUTPUT_WAREHOUSE_MISSING: 'Не задан склад выпуска',
  FROZEN_SPEC_AMBIGUOUS: 'В MRP для узла зафиксировано несколько спецификаций; маршрут неоднозначен',
  TARGET_WAREHOUSE_MISSING: 'Не задан склад точки потребления',
  ROOT_FROZEN_BOM_MISSING: 'Нет замороженной спецификации изделия',
  CUSTODY_SNAPSHOT_MISSING: 'Нет принятого снимка фактических остатков',
  FROZEN_BOM_SCHEMA_OUTDATED: 'MRP создан до корневой заморозки; требуется rebase',
  FROZEN_BOM_NODE_MISSING: 'В заморозке отсутствует узел маршрута',
  FROZEN_ROOT_SPEC_AMBIGUOUS: 'В заморозке неоднозначна спецификация изделия',
  INVALID_COMPONENT_NORM: 'Некорректная норма компонента',
  NO_SPEC: 'Не задана спецификация',
  NO_PRODUCTION_KIND: 'В спецификации не задан вид производства',
  KIND_NOT_BOUND: 'Вид производства не привязан к участку',
  NO_WAREHOUSE_BINDING: 'У участка не настроены склады материалов и результата',
  NON_STOCK_ITEM: 'Позиция является услугой, работой или операцией и не имеет складского остатка',
  ASSEMBLY_RATE_MISSING: 'Не настроен такт финишной сборки',
}

function curveQty(slot: DrumEntry, horizon: string) {
  const point = (slot.readiness_curve ?? []).find((row) => row.horizon === horizon)
  return String(point?.cumulative_qty ?? '0')
}

function coverageRoute(source: DrumCoverageSource) {
  const warehouse = source.warehouse_name || source.warehouse_ref1c
  const destination = source.destination_warehouse_name || source.destination_warehouse_ref1c
  if (warehouse && destination) {
    return `${warehouse} → ${destination}`
  }
  return warehouse || destination || 'склад не указан'
}

function firstBlockerText(slot: DrumEntry) {
  const blocker = (slot.blocking_manifest ?? [])[0]
  if (!blocker) return ''
  const item = blocker.item_article || blocker.item_code || blocker.item_name || 'данные обеспечения'
  const shortage = blocker.shortage_qty ? `, −${blocker.shortage_qty}` : ''
  return `Нет: ${item}${shortage}`
}

function blockerSearchValue(blocker: DrumSlot['blocking_manifest'][number]) {
  return blocker.item_article || blocker.item_code || blocker.item_name || ''
}

function readinessDateText(slot: DrumEntry) {
  if (slot.readiness_date) return `готовность ${slot.readiness_date}`
  const blocker = (slot.blocking_manifest ?? [])[0]
  if (blocker) return `даты нет: ${blockerLabels[blocker.reason] ?? blocker.reason}`
  const reason = (slot.unavailable_reasons ?? [])[0]
  return reason ? `даты нет: ${blockerLabels[reason] ?? reason}` : 'дата готовности не определена'
}

function groupActions(actions: DrumAction[]) {
  return actions.reduce<Record<string, DrumAction[]>>((groups, action) => {
    const key = action.action_kind
    const group = groups[key] ?? []
    group.push(action)
    groups[key] = group
    return groups
  }, {})
}

function dayLabel(iso: string) {
  const date = new Date(`${iso}T00:00:00`)
  const weekday = date.toLocaleDateString('ru-RU', { weekday: 'short' })
  const [, month, day] = iso.split('-')
  return `${day}.${month} ${weekday}`
}

function gapReason(gap: DrumGap) {
  if (gap.readiness_phase === 'blocked' || gap.readiness_phase === 'unavailable' || gap.readiness_phase === 'mixed') {
    const reasons = [...new Set((gap.blocking_manifest ?? []).map((row) => blockerLabels[row.reason] ?? row.reason))]
    return reasons.length ? reasons.join('; ') : phaseLabels[gap.readiness_phase === 'mixed' ? 'blocked' : gap.readiness_phase]
  }
  return `Готово к размещению (${phaseLabels[gap.readiness_phase]}), но не хватило мощности в горизонте`
}

function isWeekend(iso: string) {
  const weekday = new Date(`${iso}T00:00:00`).getDay()
  return weekday === 0 || weekday === 6
}

export function DrumSchedulePanel() {
  const [response, setResponse] = useState<DrumScheduleResponse | null>(null)
  const [activeEntry, setActiveEntry] = useState<DrumEntry | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [draggedSlotId, setDraggedSlotId] = useState<number | null>(null)
  const [movingSlotId, setMovingSlotId] = useState<number | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      setResponse(await listDrumSchedule(signal))
    } catch (nextError) {
      if (signal?.aborted) return
      setResponse(null)
      setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить барабан')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const handleDrop = useCallback(async (slotId: number | null, resourceId: number, day: string) => {
    const slot = response?.slots.find((row) => row.slot_id === slotId)
    setDraggedSlotId(null)
    if (!slot || slot.resource_id !== resourceId || slot.slot_date === day) return
    setMovingSlotId(slot.slot_id)
    setError('')
    setMessage('')
    try {
      const result = await moveDrumSlot(slot.slot_id, day, resourceId)
      setMessage(result.moved ? `Плитка перенесена на ${day}` : 'Плитка осталась на месте')
      await load()
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось перенести плитку')
    } finally {
      setMovingSlotId(null)
    }
  }, [load, response?.slots])

  const slotsByCell = useMemo(() => {
    const grouped = new Map<string, DrumSlot[]>()
    for (const slot of response?.slots ?? []) {
      const key = `${slot.resource_id}:${slot.slot_date}`
      const rows = grouped.get(key)
      if (rows) rows.push(slot)
      else grouped.set(key, [slot])
    }
    return grouped
  }, [response?.slots])

  const activeIsGap = activeEntry != null && 'gap_id' in activeEntry
  const activeQty = activeEntry == null
    ? 0
    : activeIsGap
      ? (activeEntry as DrumGap).gap_qty
      : (activeEntry as DrumSlot).slot_qty
  const activeDate = activeEntry == null
    ? ''
    : activeIsGap
      ? (activeEntry as DrumGap).gap_date
      : (activeEntry as DrumSlot).slot_date

  return (
    <div className="drumBoard">
      <div className="commandBar drumBoardBar">
        <button type="button" onClick={() => void load()} disabled={loading}>Обновить снимок</button>
        <span className="toolbarText">Плитки можно перетаскивать мышкой между рабочими днями своего участка</span>
        <div className="commandBarSpacer" />
        {response && <span className="toolbarText">Горизонт {response.schedule_from} — {response.schedule_to}</span>}
      </div>

      {message && <div className="drumMoveMessage" role="status">{message}</div>}
      {error && response && <div className="drumMoveError" role="alert">{error}</div>}

      {response && (
        <div className="drumKpis">
          <div className="drumKpi"><span>Открыто</span><strong>{response.total_open_qty}</strong></div>
          <div className="drumKpi"><span>В плитках</span><strong>{response.total_slot_qty}</strong></div>
          <div className={`drumKpi ${response.total_gap_qty > 0 ? 'alert' : ''}`}><span>За горизонтом</span><strong>{response.total_gap_qty}</strong></div>
          <div className={`drumKpi ${response.total_excluded > 0 ? 'alert' : ''}`}><span>Вне барабана · без такта</span><strong>{response.total_excluded_open_qty}</strong></div>
          <div className="drumKpi"><span>Плиток</span><strong>{response.total_slots}</strong></div>
        </div>
      )}

      <AsyncState
        loading={loading}
        error={error}
        empty={!response?.days.length || !response?.resources.length}
        loadingLabel="Загрузка барабана…"
        emptyLabel="В барабане нет календарных дорожек"
        onRetry={() => void load()}
      >
        <div className="drumGridWrap">
          <table className="drumGrid" aria-label="Календарный барабан сборки">
            <thead>
              <tr>
                <th className="drumResourceCol">Участок</th>
                {response?.days.map((day) => (
                  <th key={day} className={`drumDayCol${isWeekend(day) ? ' weekend' : ''}`}>{dayLabel(day)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {response?.resources.map((resource) => (
                <tr key={resource.resource_id}>
                  <td className="drumResourceCol"><div className="drumResourceName">{resource.resource_name}</div></td>
                  {response.days.map((day) => (
                    <td
                      key={day}
                      aria-label={`${resource.resource_name}, ${day}`}
                      className={`drumDayCol${draggedSlotId ? ' dropArmed' : ''}`}
                      onDragOver={(event) => {
                        const dragged = response.slots.find((slot) => slot.slot_id === draggedSlotId)
                        if (dragged?.resource_id !== resource.resource_id) return
                        event.preventDefault()
                        event.dataTransfer.dropEffect = 'move'
                      }}
                      onDrop={(event) => {
                        event.preventDefault()
                        const transferred = Number(event.dataTransfer.getData('text/plain'))
                        const slotId = Number.isFinite(transferred) && transferred > 0 ? transferred : draggedSlotId
                        void handleDrop(slotId, resource.resource_id, day)
                      }}
                    >
                      <div className="drumCell">
                        {(slotsByCell.get(`${resource.resource_id}:${day}`) ?? []).map((slot) => (
                          <button
                            type="button"
                            key={slot.slot_id}
                            className={`drumTile ${phaseClasses[slot.readiness_phase]}`}
                            draggable={movingSlotId !== slot.slot_id}
                            aria-label={`${slot.item_name || slot.item_code || `Изделие ${slot.item_id}`}: ${slot.slot_qty} шт., ${phaseLabels[slot.readiness_phase]}`}
                            onClick={() => setActiveEntry(slot)}
                            onDragStart={(event) => {
                              setDraggedSlotId(slot.slot_id)
                              event.dataTransfer.effectAllowed = 'move'
                              event.dataTransfer.setData('text/plain', String(slot.slot_id))
                            }}
                            onDragEnd={() => setDraggedSlotId(null)}
                          >
                            <span className="drumTileQty">{slot.slot_qty} шт.</span>
                            <span className="drumTileName">{slot.item_name || slot.item_code || `Изделие #${slot.item_id}`}</span>
                            {slot.item_code && slot.item_code !== slot.item_name && <span className="drumTileCode">{slot.item_code}</span>}
                            <OutputFactSummary scope="plan" planned={slot.planned_output_qty} accepted={slot.accepted_plan_output_qty} remaining={slot.assembly_remaining_qty} available={response.truth_meta.truth_status === 'accepted'} compact />
                            <span className="drumTileReadiness">
                              <span className="drumTileToday"><b>Сегодня можно</b> {curveQty(slot, 'now')} из {slot.slot_qty}</span>
                              <span><b>С комплектовкой</b> {curveQty(slot, 'kitting')} из {slot.slot_qty}</span>
                              <span><b>После запуска</b> {curveQty(slot, 'launch')} из {slot.slot_qty}</span>
                            </span>
                            {firstBlockerText(slot) && <span className="drumTileFirstBlocker">{firstBlockerText(slot)}</span>}
                            <span className="drumTileDate">{readinessDateText(slot)}</span>
                            {!!slot.blocking_manifest.length && (
                              <span className="drumTileBlockers">не закрыто позиций: {slot.blocking_manifest.length}</span>
                            )}
                            {slot.manual_override && <span className="drumTileManual">перенесено вручную</span>}
                          </button>
                        ))}
                      </div>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncState>

      {!!response?.gaps.length && (
        <details className="drumGaps">
          <summary>
            <span>Вне календаря</span>
            <span className="drumGapsSummary">{response.total_gaps} строк · {response.total_gap_qty} шт. не размещено</span>
          </summary>
          <div className="drumGapsTableWrap">
            <table className="journalTable" aria-label="Строки барабана вне календаря">
              <thead><tr><th>Контрольная дата</th><th>Участок</th><th>Изделие</th><th>Почему не размещено</th><th className="numCell">Остаток строки</th><th className="numCell">Свободная мощность</th><th className="numCell">Не размещено</th><th>Карточка</th></tr></thead>
              <tbody>
                {response.gaps.map((gap) => (
                  <tr key={gap.gap_id} className="drumGapRow">
                    <td>{gap.gap_date}</td>
                    <td>{response.resources.find((resource) => resource.resource_id === gap.resource_id)?.resource_name ?? `#${gap.resource_id}`}</td>
                    <td>
                      {gap.item_name || gap.item_code || `Изделие #${gap.item_id}`}
                      <OutputFactSummary scope="plan" planned={gap.planned_output_qty} accepted={gap.accepted_plan_output_qty} remaining={gap.assembly_remaining_qty} available={response.truth_meta.truth_status === 'accepted'} compact />
                    </td>
                    <td>{gapReason(gap)}</td>
                    <td className="numCell">{gap.required_qty}</td>
                    <td className="numCell">{gap.available_capacity}</td>
                    <td className="numCell"><strong>{gap.gap_qty}</strong></td>
                    <td><button type="button" onClick={() => setActiveEntry(gap)}>Подробнее</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {!!response?.excluded.length && (
        <details className="drumGaps drumExcluded" open>
          <summary>
            <span>Вне барабана — не настроен такт</span>
            <span className="drumGapsSummary">{response.total_excluded} строк · {response.total_excluded_open_qty} шт.</span>
          </summary>
          <div className="drumGapsTableWrap">
            <table className="journalTable" aria-label="Строки очереди без такта сборки">
              <thead><tr><th>Изделие</th><th>Readiness</th><th className="numCell">По плану</th><th className="numCell">Принято Ledger</th><th className="numCell">Осталось</th><th>Что исправить</th></tr></thead>
              <tbody>
                {response.excluded.map((row: DrumExcluded) => (
                  <tr key={row.queue_line_id} className="drumExcludedRow">
                    <td><b>{row.item_name || row.item_code || `Изделие #${row.item_id}`}</b>{row.item_code && <small>{row.item_code}</small>}</td>
                    <td>{row.readiness_status}{row.readiness_date ? ` · ${row.readiness_date}` : ''}</td>
                    <td className="numCell">{row.planned_output_qty}</td>
                    <td className="numCell">{row.accepted_plan_output_qty}</td>
                    <td className="numCell"><strong>{row.assembly_remaining_qty}</strong></td>
                    <td>{blockerLabels[row.reason] ?? row.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {activeEntry && (
        <div className="dialogOverlay" role="dialog" aria-modal="true" aria-label={`${activeIsGap ? 'Вне календаря' : 'Плитка'}: ${activeEntry.item_name || activeEntry.item_code}`} onClick={() => setActiveEntry(null)}>
          <div className="dialogBox drumSlotDialog" onClick={(event) => event.stopPropagation()}>
            <div className="dialogHeader">{activeIsGap ? 'Вне календаря' : 'Плитка'}: {activeEntry.item_name || activeEntry.item_code}</div>
            <div className="dialogBody">
              <div className="drumTileDetails">
                <div><b>{activeIsGap ? 'Контрольная дата' : 'Дата'}:</b> {activeDate}</div>
                <div><b>Участок:</b> {response?.resources.find((resource) => resource.resource_id === activeEntry.resource_id)?.resource_name ?? activeEntry.resource_id}</div>
                <div><b>Номенклатура:</b> {activeEntry.item_code || '—'} — {activeEntry.item_name || '—'}</div>
                <div><b>{activeIsGap ? 'Не размещено' : 'Количество'}:</b> {activeQty}</div>
                <OutputFactSummary scope="plan" planned={activeEntry.planned_output_qty} accepted={activeEntry.accepted_plan_output_qty} remaining={activeEntry.assembly_remaining_qty} available={response?.truth_meta.truth_status === 'accepted'} />
                <div><b>Readiness gate:</b> {phaseLabels[activeEntry.readiness_phase] ?? activeEntry.readiness_phase}</div>
                {activeIsGap && <div><b>Почему вне календаря:</b> {gapReason(activeEntry as DrumGap)}</div>}
                <div><b>План / строка:</b> {activeEntry.plan_id} / {activeEntry.plan_line_id}</div>
                <div><b>MRP:</b> {activeEntry.run_id ?? '—'}</div>
                <div><b>Период:</b> {activeEntry.period_from && activeEntry.period_to ? `${activeEntry.period_from} — ${activeEntry.period_to}` : '—'}</div>
                <div><b>Исходный приоритет:</b> {activeEntry.original_priority.join(' / ') || '—'}</div>
                {!activeIsGap && (activeEntry as DrumSlot).manual_override && (
                  <div><b>Ручной перенос:</b> {(activeEntry as DrumSlot).manual_moved_by || 'оператор не указан'}{(activeEntry as DrumSlot).manual_moved_at ? ` · ${(activeEntry as DrumSlot).manual_moved_at}` : ''}</div>
                )}
              </div>
              <section className={`drumTodayAnswer ${phaseClasses[activeEntry.readiness_phase] ?? 'gateUnavailable'}`} aria-label="Ответ на текущий день">
                <strong>Сегодня можно собрать {curveQty(activeEntry, 'now')} из {activeQty}</strong>
                {!!activeEntry.blocking_manifest?.length && <span>На остаток не закрыто позиций: {activeEntry.blocking_manifest.length}</span>}
              </section>
              <section className="drumReadinessLadder" aria-label="Лестница готовности">
                <h3>Что можно собрать и при каких условиях</h3>
                <div className="drumReadinessScope">Количество относится к {activeIsGap ? 'неразмещённому остатку' : 'плитке'}; причины и необходимые действия — ко всему остатку строки.</div>
                {(activeEntry.readiness_curve ?? []).map((point) => (
                  <div className="drumReadinessStep" key={point.horizon}>
                    <div className="drumReadinessStepHead">
                      <b>{horizonLabels[point.horizon] ?? point.horizon}</b>
                      <strong>{point.cumulative_qty} из {activeQty}</strong>
                      <span>{point.available_date || 'даты нет'}</span>
                    </div>
                    {!!point.required_actions?.length && (
                      <div>Нужно сделать: {point.required_actions.map((action) => `${actionLabels[action.action_kind] ?? action.action_kind} ${action.item_article || action.item_code || action.item_name || `#${action.item_id}`} — ${action.qty} шт.`).join('; ')}</div>
                    )}
                    {!!point.blockers?.length && (
                      <div className="drumReadinessStepBlocked">Не закрыто: {point.blockers.map((blocker) => `${blocker.item_article || blocker.item_code || blocker.item_name || `#${blocker.item_id}`} — ${blockerLabels[blocker.reason] ?? blocker.reason}${blocker.shortage_qty ? `, −${blocker.shortage_qty}` : ''}`).join('; ')}</div>
                    )}
                  </div>
                ))}
              </section>
              {!!activeEntry.action_manifest?.length && (
                <div className="drumActionGroups">
                  {Object.entries(groupActions(activeEntry.action_manifest)).map(([kind, actions]) => (
                    <section key={kind} className="drumActionGroup">
                      <h3>{actionLabels[kind] ?? kind}</h3>
                      {actions.map((action, index) => (
                        <div className="drumActionRow" key={`${kind}:${action.item_id}:${index}`}>
                          <div><b>{action.item_article || action.item_code || `#${action.item_id}`}</b> — {action.item_name || 'Без наименования'}</div>
                          <div>
                            {action.qty} шт.
                            {(action.source_warehouse_name || action.source_warehouse_ref1c) && ` · ${action.source_warehouse_name || action.source_warehouse_ref1c}`}
                            {(action.destination_warehouse_name || action.destination_warehouse_ref1c) && ` → ${action.destination_warehouse_name || action.destination_warehouse_ref1c}`}
                            {action.resource_id != null && ` · ${action.resource_name || `участок #${action.resource_id}`}`}
                            {action.available_date ? ` · к ${action.available_date}` : ''}
                            {action.confidence && ` · ${confidenceLabels[action.confidence] ?? action.confidence}`}
                          </div>
                        </div>
                      ))}
                    </section>
                  ))}
                </div>
              )}
              {!!activeEntry.unavailable_reasons?.length && (
                <div className="errorBox">Расчёт закрыт: {activeEntry.unavailable_reasons.join(', ')}</div>
              )}
              {!!activeEntry.blocking_manifest?.length && (
                <section className="drumBlockers">
                  <h3>Что мешает собрать остаток</h3>
                  <div className="drumDeficitTableWrap">
                    <table className="drumDeficitTable" aria-label="Дефициты по источникам обеспечения">
                      <thead>
                        <tr>
                          <th>Позиция</th>
                          <th>Требуется</th>
                          <th>В точке потребления</th>
                          <th>Передано участку</th>
                          <th>В пути</th>
                          <th>В производстве</th>
                          <th>Поставка</th>
                          <th>На других складах</th>
                          <th>Не хватает</th>
                          <th>Причина</th>
                        </tr>
                      </thead>
                      <tbody>
                        {activeEntry.blocking_manifest.map((blocker, index) => (
                          <Fragment key={`${blocker.item_id ?? 'reason'}:${blocker.reason}:${index}`}>
                            <tr className="drumBlockerRow">
                              <td>
                                <b>{blocker.item_article || blocker.item_code || blocker.item_name || 'Данные обеспечения'}</b>
                                {blocker.item_name && <small>{blocker.item_name}</small>}
                                {(blocker.destination_warehouse_name || blocker.destination_warehouse_ref1c) && <small>Точка потребления: {blocker.destination_warehouse_name || blocker.destination_warehouse_ref1c}</small>}
                                {blockerSearchValue(blocker) && (
                                  <span className="drumBlockerLinks">
                                    <a href={`#/production-control?search=${encodeURIComponent(blockerSearchValue(blocker))}`}>Журнал</a>
                                    <a href={`#/production-control?view=mechshop&search=${encodeURIComponent(blockerSearchValue(blocker))}`}>Очередь мехцеха</a>
                                  </span>
                                )}
                              </td>
                              <td>{blocker.required_qty ?? '—'}</td>
                              <td>{blocker.point_of_use_qty}</td>
                              <td>{blocker.custody_qty}</td>
                              <td>{blocker.transit_qty}</td>
                              <td>{blocker.wip_qty}</td>
                              <td>{blocker.supplier_qty}</td>
                              <td>{blocker.other_stock_qty}</td>
                              <td><strong>{blocker.shortage_qty ?? '—'}</strong></td>
                              <td>{blockerLabels[blocker.reason] ?? blocker.reason}</td>
                            </tr>
                            {!!blocker.coverage_sources?.length && (
                              <tr className="drumCoverageDetailRow">
                                <td colSpan={10}>
                                  <div className="drumCoverageSources" aria-label={`Источники покрытия: ${blocker.item_article || blocker.item_code || blocker.item_name || 'позиция'}`}>
                                    {blocker.coverage_sources.map((source, sourceIndex) => (
                                      <div key={`${source.source_key}:${source.coverage_kind}:${sourceIndex}`}>
                                        <b>{coverageKindLabels[source.coverage_kind] ?? source.coverage_kind}</b>
                                        {' · '}{source.qty} шт. · {coverageRoute(source)}
                                        {source.source_ref && ` · ${source.source_ref}`}
                                        {source.available_date && ` · к ${source.available_date}`}
                                        {source.confidence && ` · ${confidenceLabels[source.confidence] ?? source.confidence}`}
                                      </div>
                                    ))}
                                  </div>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="drumDeficitScope">Все количества рассчитаны backend по всему недовыпущенному остатку строки после потребления старшими плитками.</div>
                </section>
              )}
            </div>
            <div className="dialogFooter"><button type="button" onClick={() => setActiveEntry(null)}>Закрыть</button></div>
          </div>
        </div>
      )}
    </div>
  )
}
