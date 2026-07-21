import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DbrBoard, DbrBoardSlot, DbrProgram, DbrReleaseDayResult, DbrReleaseResult } from '../../../domain/dbr'
import { dateRu, isoToday, shiftIsoDate } from '../../../lib/format'
import {
  activateDbrDrum, buildDbrDrum, getDbrBoard, listDbrPrograms, moveDbrSlot,
  refreshDbrGate, releaseDbrDay, releaseDbrSlot, rollForwardDbrDrum,
} from '../../../services/dbr'
import { groupDrumSlotsByCell, indexDrumSlotsById } from './model'

export function useDbrDrumBoardController() {
  const loadSeq = useRef(0)
  const mutationLocked = useRef(false)
  const releaseLocked = useRef(false)
  const dayLocked = useRef(false)
  const [board, setBoard] = useState<DbrBoard | null>(null)
  const [dateFrom, setDateFrom] = useState(shiftIsoDate(isoToday(), -2))
  const [dateTo, setDateTo] = useState(shiftIsoDate(isoToday(), 14))
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [selectedSlot, setSelectedSlot] = useState<DbrBoardSlot | null>(null)
  const [moveDate, setMoveDate] = useState('')
  const [moveResource, setMoveResource] = useState('')
  const [buildOpen, setBuildOpen] = useState(false)
  const [approvedPrograms, setApprovedPrograms] = useState<DbrProgram[]>([])
  const [buildProgramId, setBuildProgramId] = useState('')
  const [releaseFlow, setReleaseFlow] = useState<{ slot: DbrBoardSlot; preview: DbrReleaseResult; result?: DbrReleaseResult } | null>(null)
  const [releaseBusy, setReleaseBusy] = useState(false)
  const [releaseError, setReleaseError] = useState('')
  const [dayModal, setDayModal] = useState<{ phase: 'pick' | 'preview' | 'done'; day: string; preview?: DbrReleaseDayResult; result?: DbrReleaseDayResult } | null>(null)
  const [dayBusy, setDayBusy] = useState(false)
  const [dayError, setDayError] = useState('')

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setLoading(true); setError('')
    try {
      const next = await getDbrBoard({ date_from: dateFrom, date_to: dateTo })
      if (seq === loadSeq.current) setBoard(next)
    } catch (e) {
      if (seq === loadSeq.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [dateFrom, dateTo])
  useEffect(() => { void load() }, [load])

  const schedule = board?.schedule ?? null
  const slotsByCell = useMemo(() => groupDrumSlotsByCell(board?.slots ?? []), [board])
  const slotById = useMemo(() => indexDrumSlotsById(board?.slots ?? []), [board])

  function openSlot(slot: DbrBoardSlot) {
    setSelectedSlot(slot); setMoveDate(slot.date); setMoveResource(String(slot.resource_id)); setMessage(''); setError('')
  }
  function beginMutation() {
    if (mutationLocked.current) return false
    mutationLocked.current = true; setSaving(true); setError(''); setMessage(''); return true
  }
  function endMutation() { mutationLocked.current = false; setSaving(false) }

  async function refreshGate() {
    if (!schedule || !beginMutation()) return
    try {
      const res = await refreshDbrGate(schedule.id)
      setMessage(`Гейт обновлён: 🟢 ${res.green} · 🟡 ${res.yellow} · 🔴 ${res.red} (изменено ${res.updated})`)
      await load()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { endMutation() }
  }
  async function rollForward() {
    if (!schedule || !beginMutation()) return
    try {
      const res = await rollForwardDbrDrum(schedule.id)
      if (res.horizon_exhausted) setMessage('Горизонт графика исчерпан — постройте новый период')
      else setMessage(`Перенесено плиток: ${res.moved} · закрыто выпуском: ${res.closed}${res.overloaded ? ` · с перегрузом: ${res.overloaded}` : ''}`)
      await load()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { endMutation() }
  }
  async function openBuild() {
    setBuildOpen(true); setError('')
    try {
      const list = await listDbrPrograms('approved')
      setApprovedPrograms(list); setBuildProgramId(list.length ? String(list[0].id) : '')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  async function runBuild() {
    const programId = Number(buildProgramId)
    if (!programId) { setError('Выберите утверждённую программу'); return }
    if (!beginMutation()) return
    try {
      const built = await buildDbrDrum(programId)
      await activateDbrDrum(built.schedule.id)
      const carried = built.carried_over?.length ?? 0
      setMessage(`График №${built.schedule.id} построен и активирован${carried ? ` · перенесено на след. период: ${carried}` : ''}`)
      setBuildOpen(false); await load()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { endMutation() }
  }
  async function doMove() {
    if (!selectedSlot || !beginMutation()) return
    try {
      const resourceId = Number(moveResource)
      const res = await moveDbrSlot(selectedSlot.id, moveDate, resourceId !== selectedSlot.resource_id ? resourceId : undefined)
      setMessage(res.moved ? `Плитка перенесена на ${dateRu(res.to)}` : 'Плитка осталась на месте')
      setSelectedSlot(null); await load()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { endMutation() }
  }
  async function startRelease() {
    if (!selectedSlot || !beginMutation()) return
    setReleaseError('')
    try {
      const preview = await releaseDbrSlot(selectedSlot.id, true)
      setReleaseFlow({ slot: selectedSlot, preview })
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { endMutation() }
  }
  async function confirmRelease() {
    if (!releaseFlow || releaseLocked.current) return
    releaseLocked.current = true; setReleaseBusy(true); setReleaseError('')
    try {
      const result = await releaseDbrSlot(releaseFlow.slot.id, false)
      setReleaseFlow((prev) => (prev ? { ...prev, result } : prev)); await load()
    } catch (e) { setReleaseError(e instanceof Error ? e.message : String(e)) } finally {
      releaseLocked.current = false; setReleaseBusy(false)
    }
  }
  function closeReleaseFlow() {
    const wasDone = Boolean(releaseFlow?.result); setReleaseFlow(null); if (wasDone) setSelectedSlot(null)
  }
  function openDayRelease() { setDayError(''); setDayModal({ phase: 'pick', day: isoToday() }) }
  async function runDayPreview(day: string) {
    if (!schedule || dayLocked.current) return
    dayLocked.current = true; setDayBusy(true); setDayError('')
    try {
      const preview = await releaseDbrDay(schedule.id, day, true)
      setDayModal({ phase: 'preview', day, preview })
    } catch (e) { setDayError(e instanceof Error ? e.message : String(e)) } finally {
      dayLocked.current = false; setDayBusy(false)
    }
  }
  async function confirmDay() {
    if (!schedule || !dayModal || dayLocked.current) return
    dayLocked.current = true; setDayBusy(true); setDayError('')
    try {
      const result = await releaseDbrDay(schedule.id, dayModal.day, false)
      setDayModal((prev) => (prev ? { ...prev, phase: 'done', result } : prev)); await load()
    } catch (e) { setDayError(e instanceof Error ? e.message : String(e)) } finally {
      dayLocked.current = false; setDayBusy(false)
    }
  }

  return {
    board, dateFrom, dateTo, loading, saving, error, message, selectedSlot, moveDate, moveResource,
    buildOpen, approvedPrograms, buildProgramId, releaseFlow, releaseBusy, releaseError,
    dayModal, dayBusy, dayError, schedule, slotsByCell, slotById,
    setDateFrom, setDateTo, setSelectedSlot, setMoveDate, setMoveResource, setBuildOpen,
    setBuildProgramId, setDayModal, load, openSlot, refreshGate, rollForward, openBuild, runBuild,
    doMove, startRelease, confirmRelease, closeReleaseFlow, openDayRelease, runDayPreview, confirmDay,
  }
}
