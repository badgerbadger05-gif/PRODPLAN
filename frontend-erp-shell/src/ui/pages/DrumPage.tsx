import { useCallback, useEffect, useState } from 'react'
import { dateRu, qty } from '../../lib/format'
import { unavailableTruth } from '../../lib/api'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { TruthUnavailableNotice } from '../TruthUnavailableNotice'
import { listDrum } from '../../services/drum'
import type { DrumCapacityGapRow, DrumSlotRow } from '../../domain/drum'

const PAGE_LIMIT = 200

function formatPriority(key: Array<string | number>) {
  return key.join(' · ')
}

function formatDate(value: string) {
  return dateRu(value)
}

function formatNumber(value: number) {
  return qty(value)
}

export function DrumPage() {
  const [slots, setSlots] = useState<DrumSlotRow[]>([])
  const [gaps, setGaps] = useState<DrumCapacityGapRow[]>([])
  const [totalSlots, setTotalSlots] = useState(0)
  const [totalGaps, setTotalGaps] = useState(0)
  const [offset, setOffset] = useState(0)
  const [totalOpenQty, setTotalOpenQty] = useState(0)
  const [totalSlotQty, setTotalSlotQty] = useState(0)
  const [totalGapQty, setTotalGapQty] = useState(0)
  const [scheduleFrom, setScheduleFrom] = useState('')
  const [scheduleTo, setScheduleTo] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [unavailable, setUnavailable] = useState('')

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setUnavailable('')
    try {
      const data = await listDrum({ limit: PAGE_LIMIT, offset: nextOffset })
      setSlots(data.slots ?? [])
      setGaps(data.gaps ?? [])
      setTotalSlots(Number(data.total_slots || 0))
      setTotalGaps(Number(data.total_gaps || 0))
      setOffset(Number(data.offset ?? nextOffset))
      setTotalOpenQty(Number(data.total_open_qty || 0))
      setTotalSlotQty(Number(data.total_slot_qty || 0))
      setTotalGapQty(Number(data.total_gap_qty || 0))
      setScheduleFrom(data.schedule_from || '')
      setScheduleTo(data.schedule_to || '')
    } catch (e) {
      const blocked = unavailableTruth(e)
      if (blocked) setUnavailable(blocked.reason)
      else setError(e instanceof Error ? e.message : String(e))
      setSlots([])
      setGaps([])
      setTotalSlots(0)
      setTotalGaps(0)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(0)
  }, [load])

  // The endpoint windows slots and gaps independently with the same
  // limit/offset, so the pager walks whichever collection is longer.
  const pagedTotal = Math.max(totalSlots, totalGaps)
  const visibleRows = Math.max(slots.length, gaps.length)
  const visibleFrom = visibleRows ? offset + 1 : 0
  const visibleTo = offset + visibleRows

  return (
    <main className="workArea">
      <DocumentWindow
        title="Барабан"
        subtitle="Сырые слоты барабана и явные дефициты мощности по дате"
        hotkeys="Оба блока — готовый persisted read-model, пересчёт не выполняется на клиенте"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={pagedTotal}
            selectedCount={0}
            canPrev={offset > 0}
            canNext={visibleTo < pagedTotal}
            onPrev={() => void load(Math.max(0, offset - PAGE_LIMIT))}
            onNext={() => void load(offset + PAGE_LIMIT)}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => void load(offset)} disabled={loading}>Обновить</button>
          <div className="toolbarText">
            {scheduleFrom && scheduleTo ? `Окно барабана: ${formatDate(scheduleFrom)}–${formatDate(scheduleTo)}` : 'Окно барабана: не рассчитано'}
          </div>
          <div className="toolbarText">
            Открытое: {formatNumber(totalOpenQty)} | Слоты: {formatNumber(totalSlotQty)} | Дефицит: {formatNumber(totalGapQty)}
          </div>
        </div>

        {unavailable && <TruthUnavailableNotice reason={unavailable} />}
        {error && <div className="errorLine">{error}</div>}

        <h3>Слоты барабана — {totalSlots}</h3>
        <div style={{ overflow: 'auto', marginBottom: 20 }}>
          <table className="journalTable">
            <thead>
              <tr>
                <th className="numCell">plan</th>
                <th className="numCell">plan_line</th>
                <th className="numCell">item_id</th>
                <th className="numCell">resource_id</th>
                <th>Дата слота</th>
                <th className="numCell">Порядок</th>
                <th className="numCell">Количество</th>
                <th>Приоритет</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={8}>Загрузка...</td>
                </tr>
              ) : null}
              {!loading && !slots.length ? (
                <tr>
                  <td colSpan={8}>Слоты отсутствуют</td>
                </tr>
              ) : null}
              {!loading && slots.map((slot) => (
                <tr key={`${slot.plan_id}-${slot.plan_line_id}-${slot.resource_id}-${slot.slot_ordinal}`}>
                  <td className="numCell">{slot.plan_id}</td>
                  <td className="numCell">{slot.plan_line_id}</td>
                  <td className="numCell">{slot.item_id}</td>
                  <td className="numCell">{slot.resource_id}</td>
                  <td>{formatDate(slot.slot_date)}</td>
                  <td className="numCell">{slot.slot_ordinal}</td>
                  <td className="numCell">{formatNumber(slot.slot_qty)}</td>
                  <td>{formatPriority(slot.original_priority)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <h3>Дефицит мощности (gaps) — {totalGaps}</h3>
        <div style={{ overflow: 'auto' }}>
          <table className="journalTable">
            <thead>
              <tr>
                <th className="numCell">plan</th>
                <th className="numCell">plan_line</th>
                <th className="numCell">item_id</th>
                <th className="numCell">resource_id</th>
                <th>Дата дефицита</th>
                <th className="numCell">Дефицит</th>
                <th>Приоритет</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={7}>Загрузка...</td>
                </tr>
              ) : null}
              {!loading && !gaps.length ? (
                <tr>
                  <td colSpan={7}>Дефицит мощности не найден</td>
                </tr>
              ) : null}
              {!loading && gaps.map((gap) => (
                <tr key={`${gap.plan_id}-${gap.plan_line_id}-${gap.resource_id}-${gap.gap_date}`}>
                  <td className="numCell">{gap.plan_id}</td>
                  <td className="numCell">{gap.plan_line_id}</td>
                  <td className="numCell">{gap.item_id}</td>
                  <td className="numCell">{gap.resource_id}</td>
                  <td>{formatDate(gap.gap_date)}</td>
                  <td className="numCell">{formatNumber(gap.gap_qty)}</td>
                  <td>{formatPriority(gap.original_priority)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
