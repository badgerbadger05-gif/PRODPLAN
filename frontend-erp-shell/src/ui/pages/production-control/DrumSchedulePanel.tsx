import { useCallback, useEffect, useMemo, useState } from 'react'

import { listDrumSchedule, moveDrumSlot, type DrumScheduleResponse } from '../../../services/productionControl'
import { AsyncState } from '../../layout/AsyncState'

type DrumSlot = DrumScheduleResponse['slots'][number]

const phaseLabels: Record<DrumSlot['readiness_phase'], string> = {
  now: 'Можно собирать сейчас',
  transfer: 'После перемещения',
  kitting: 'После комплектовки',
  committed: 'После подтверждённого поступления',
  launch: 'После запуска обеспечения',
  blocked: 'Пока не собирается',
  unavailable: 'Недостаточно данных',
}

const phaseClasses: Record<DrumSlot['readiness_phase'], string> = {
  now: 'gateReady',
  transfer: 'gateTransfer',
  kitting: 'gateKitting',
  committed: 'gateCommitted',
  launch: 'gateLaunch',
  blocked: 'gateBlocked',
  unavailable: 'gateUnavailable',
}

const actionLabels: Record<string, string> = {
  transfer: 'Переместить',
  kitting: 'Скомплектовать',
  make: 'Запустить узел / ДСЕ',
  rework: 'Запустить переработку',
  buy: 'Закупить',
  committed_supply: 'Подтверждённое поступление',
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
  TARGET_WAREHOUSE_MISSING: 'Не задан склад точки потребления',
  ROOT_FROZEN_BOM_MISSING: 'Нет замороженной спецификации изделия',
  CUSTODY_SNAPSHOT_MISSING: 'Нет принятого снимка фактических остатков',
}

function curveQty(slot: DrumSlot, horizon: string) {
  const point = slot.readiness_curve.find((row) => row.horizon === horizon)
  return String(point?.cumulative_qty ?? '0')
}

function groupActions(actions: DrumSlot['action_manifest']) {
  return actions.reduce<Record<string, DrumSlot['action_manifest']>>((groups, action) => {
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

function isWeekend(iso: string) {
  const weekday = new Date(`${iso}T00:00:00`).getDay()
  return weekday === 0 || weekday === 6
}

export function DrumSchedulePanel() {
  const [response, setResponse] = useState<DrumScheduleResponse | null>(null)
  const [activeSlot, setActiveSlot] = useState<DrumSlot | null>(null)
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

  const handleDrop = useCallback(async (resourceId: number, day: string) => {
    const slot = response?.slots.find((row) => row.slot_id === draggedSlotId)
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
  }, [draggedSlotId, load, response?.slots])

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

  return (
    <div className="drumBoard">
      <div className="commandBar drumBoardBar">
        <button type="button" onClick={() => void load()} disabled={loading}>Обновить снимок</button>
        <span className="toolbarText">Плитки можно перетаскивать мышкой между рабочими днями своего участка</span>
        <div className="commandBarSpacer" />
        {response && <span className="toolbarText">Горизонт {response.schedule_from} — {response.schedule_to}</span>}
      </div>

      {message && <div className="drumMoveMessage" role="status">{message}</div>}

      {response && (
        <div className="drumKpis">
          <div className="drumKpi"><span>Открыто</span><strong>{response.total_open_qty}</strong></div>
          <div className="drumKpi"><span>В плитках</span><strong>{response.total_slot_qty}</strong></div>
          <div className={`drumKpi ${response.total_gap_qty > 0 ? 'alert' : ''}`}><span>За горизонтом</span><strong>{response.total_gap_qty}</strong></div>
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
                        void handleDrop(resource.resource_id, day)
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
                            onClick={() => setActiveSlot(slot)}
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
                            <span className="drumTileReadiness">
                              <span><b>Сейчас</b> {curveQty(slot, 'now')}/{slot.slot_qty}</span>
                              <span><b>С комплектовкой</b> {curveQty(slot, 'kitting')}/{slot.slot_qty}</span>
                              <span><b>После запуска</b> {curveQty(slot, 'launch')}/{slot.slot_qty}</span>
                            </span>
                            {slot.readiness_date && <span className="drumTileDate">готовность {slot.readiness_date}</span>}
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
        <section className="drumGaps">
          <h2>Разрывы мощности</h2>
          <table className="journalTable" aria-label="Разрывы мощности барабана">
            <thead><tr><th>Дата</th><th>Участок</th><th>Изделие</th><th className="numCell">Требуется</th><th className="numCell">Доступно</th><th className="numCell">Дефицит</th></tr></thead>
            <tbody>
              {response.gaps.map((gap) => (
                <tr key={gap.gap_id} className="drumGapRow">
                  <td>{gap.gap_date}</td>
                  <td>{response.resources.find((resource) => resource.resource_id === gap.resource_id)?.resource_name ?? `#${gap.resource_id}`}</td>
                  <td>{gap.item_name || gap.item_code || `Изделие #${gap.item_id}`}</td>
                  <td className="numCell">{gap.required_qty}</td>
                  <td className="numCell">{gap.available_capacity}</td>
                  <td className="numCell"><strong>{gap.gap_qty}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {activeSlot && (
        <div className="dialogOverlay" role="dialog" aria-modal="true" aria-label={`Плитка: ${activeSlot.item_name || activeSlot.item_code}`} onClick={() => setActiveSlot(null)}>
          <div className="dialogBox" onClick={(event) => event.stopPropagation()}>
            <div className="dialogHeader">Плитка: {activeSlot.item_name || activeSlot.item_code}</div>
            <div className="dialogBody">
              <div className="drumTileDetails">
                <div><b>Дата:</b> {activeSlot.slot_date}</div>
                <div><b>Участок:</b> {response?.resources.find((resource) => resource.resource_id === activeSlot.resource_id)?.resource_name ?? activeSlot.resource_id}</div>
                <div><b>Номенклатура:</b> {activeSlot.item_code || '—'} — {activeSlot.item_name || '—'}</div>
                <div><b>Количество:</b> {activeSlot.slot_qty}</div>
                <div><b>Readiness gate:</b> {phaseLabels[activeSlot.readiness_phase]}</div>
                <div><b>Собирается сейчас:</b> {curveQty(activeSlot, 'now')} из {activeSlot.slot_qty}</div>
                <div><b>После перемещения:</b> {curveQty(activeSlot, 'transfer')} из {activeSlot.slot_qty}</div>
                <div><b>После комплектовки:</b> {curveQty(activeSlot, 'kitting')} из {activeSlot.slot_qty}</div>
                <div><b>После подтверждённых поступлений:</b> {curveQty(activeSlot, 'committed')} из {activeSlot.slot_qty}</div>
                <div><b>После запуска:</b> {curveQty(activeSlot, 'launch')} из {activeSlot.slot_qty}{activeSlot.readiness_date ? ` · ${activeSlot.readiness_date}` : ''}</div>
                <div><b>План / строка:</b> {activeSlot.plan_id} / {activeSlot.plan_line_id}</div>
              </div>
              {!!activeSlot.action_manifest.length && (
                <div className="drumActionGroups">
                  {Object.entries(groupActions(activeSlot.action_manifest)).map(([kind, actions]) => (
                    <section key={kind} className="drumActionGroup">
                      <h3>{actionLabels[kind] ?? kind}</h3>
                      {actions.map((action, index) => (
                        <div className="drumActionRow" key={`${kind}:${action.item_id}:${index}`}>
                          <div><b>{action.item_article || action.item_code || `#${action.item_id}`}</b> — {action.item_name || 'Без наименования'}</div>
                          <div>{action.qty} шт.{action.available_date ? ` · к ${action.available_date}` : ''}</div>
                        </div>
                      ))}
                    </section>
                  ))}
                </div>
              )}
              {!!activeSlot.unavailable_reasons.length && (
                <div className="errorBox">Расчёт закрыт: {activeSlot.unavailable_reasons.join(', ')}</div>
              )}
              {!!activeSlot.blocking_manifest.length && (
                <section className="drumBlockers">
                  <h3>Что мешает собрать остаток</h3>
                  {activeSlot.blocking_manifest.map((blocker, index) => (
                    <div className="drumBlockerRow" key={`${blocker.item_id ?? 'reason'}:${blocker.reason}:${index}`}>
                      <div>
                        <b>{blocker.item_article || blocker.item_code || blocker.item_name || 'Данные обеспечения'}</b>
                        {blocker.item_name && ` — ${blocker.item_name}`}
                      </div>
                      <div>{blockerLabels[blocker.reason] ?? blocker.reason}</div>
                      {blocker.shortage_qty && <div>Не хватает {blocker.shortage_qty} шт. из {blocker.required_qty ?? '—'}</div>}
                    </div>
                  ))}
                </section>
              )}
            </div>
            <div className="dialogFooter"><button type="button" onClick={() => setActiveSlot(null)}>Закрыть</button></div>
          </div>
        </div>
      )}
    </div>
  )
}
