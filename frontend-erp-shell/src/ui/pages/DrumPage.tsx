import { useCallback, useEffect, useState } from 'react'
import { dateRu, qty } from '../../lib/format'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { listDrum } from '../../services/drum'
import type { DrumCapacityGapRow, DrumSlotRow } from '../../domain/drum'

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
  const [totalOpenQty, setTotalOpenQty] = useState(0)
  const [totalSlotQty, setTotalSlotQty] = useState(0)
  const [totalGapQty, setTotalGapQty] = useState(0)
  const [scheduleFrom, setScheduleFrom] = useState('')
  const [scheduleTo, setScheduleTo] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await listDrum()
      setSlots(data.slots ?? [])
      setGaps(data.gaps ?? [])
      setTotalOpenQty(Number(data.total_open_qty || 0))
      setTotalSlotQty(Number(data.total_slot_qty || 0))
      setTotalGapQty(Number(data.total_gap_qty || 0))
      setScheduleFrom(data.schedule_from || '')
      setScheduleTo(data.schedule_to || '')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const totalRows = slots.length + gaps.length

  return (
    <main className="workArea">
      <DocumentWindow
        title="Барабан"
        subtitle="Сырые слоты барабана и явные дефициты мощности по дате"
        hotkeys="Оба блока — готовый persisted read-model, пересчёт не выполняется на клиенте"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={totalRows ? 1 : 0}
            visibleTo={totalRows}
            total={totalRows}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
          <div className="toolbarText">
            {scheduleFrom && scheduleTo ? `Окно барабана: ${formatDate(scheduleFrom)}–${formatDate(scheduleTo)}` : 'Окно барабана: не рассчитано'}
          </div>
          <div className="toolbarText">
            Открытое: {formatNumber(totalOpenQty)} | Слоты: {formatNumber(totalSlotQty)} | Дефицит: {formatNumber(totalGapQty)}
          </div>
        </div>

        {error && <div className="errorLine">{error}</div>}

        <h3>Слоты барабана</h3>
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

        <h3>Дефицит мощности (gaps)</h3>
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
