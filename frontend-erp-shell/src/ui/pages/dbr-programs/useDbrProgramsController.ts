import { useCallback, useEffect, useRef, useState } from 'react'
import type { DbrProgram } from '../../../domain/dbr'
import { isoToday, shiftIsoDate } from '../../../lib/format'
import {
  approveDbrProgram,
  createDbrProgram,
  getDbrProgram,
  listDbrPrograms,
  updateDbrProgram,
} from '../../../services/dbr'
import {
  alignFirstDraftDate,
  buildProgramCreatePayload,
  createDraftProgramRow,
  programToDraftRows,
  validateProgramItems,
} from './model'
import type { DraftProgramRow } from './model'

let rowSeq = 0
function newRow(dateDefault: string): DraftProgramRow {
  rowSeq += 1
  return createDraftProgramRow(`r${rowSeq}`, dateDefault)
}

export function useDbrProgramsController() {
  const detailRequestSeq = useRef(0)
  const mutationLocked = useRef(false)
  const [programs, setPrograms] = useState<DbrProgram[]>([])
  const [selected, setSelected] = useState<DbrProgram | null>(null)
  const [editRows, setEditRows] = useState<DraftProgramRow[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [fromDate, setFromDate] = useState(isoToday())
  const [toDate, setToDate] = useState(shiftIsoDate(isoToday(), 14))
  const [title, setTitle] = useState('')
  const [company, setCompany] = useState('')
  const [rows, setRows] = useState<DraftProgramRow[]>(() => [newRow(isoToday())])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setPrograms(await listDbrPrograms())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  async function openProgram(id: number) {
    const requestSeq = ++detailRequestSeq.current
    setError('')
    setMessage('')
    try {
      const program = await getDbrProgram(id)
      if (requestSeq !== detailRequestSeq.current) return
      setSelected(program)
      setEditRows(programToDraftRows(program))
    } catch (e) {
      if (requestSeq !== detailRequestSeq.current) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function patchRow(key: string, next: Partial<DraftProgramRow>) {
    setRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...next } : row)))
  }

  function addRow() { setRows((prev) => [...prev, newRow(fromDate)]) }
  function removeRow(key: string) {
    setRows((prev) => (prev.length > 1 ? prev.filter((row) => row.key !== key) : prev))
  }
  function changeFromDate(nextDate: string) {
    setFromDate(nextDate)
    setRows((prev) => alignFirstDraftDate(prev, nextDate))
  }

  async function submit() {
    let payload
    try {
      payload = buildProgramCreatePayload(rows, fromDate, toDate, title, company)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e)); return
    }
    if (mutationLocked.current) return
    mutationLocked.current = true
    setSaving(true); setError(''); setMessage('')
    try {
      const created = await createDbrProgram(payload)
      setMessage(`Программа №${created.id} создана (${payload.items.length} строк)`)
      setSelected(created); setEditRows(programToDraftRows(created))
      setRows([newRow(fromDate)]); setTitle('')
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      mutationLocked.current = false; setSaving(false)
    }
  }

  async function approve(id: number) {
    if (mutationLocked.current) return
    mutationLocked.current = true
    setSaving(true); setError(''); setMessage('')
    try {
      const approved = await approveDbrProgram(id)
      setMessage(`Программа №${id} утверждена`)
      setSelected(approved); setEditRows(programToDraftRows(approved))
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      mutationLocked.current = false; setSaving(false)
    }
  }

  function patchEditRow(key: string, next: Partial<DraftProgramRow>) {
    setEditRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...next } : row)))
  }
  function addEditRow() {
    if (selected) setEditRows((prev) => [...prev, newRow(selected.from_date)])
  }
  function removeEditRow(key: string) {
    setEditRows((prev) => prev.filter((row) => row.key !== key))
  }

  async function saveDraftItems() {
    if (!selected || selected.status !== 'draft') return
    let items: ReturnType<typeof validateProgramItems>
    try {
      items = validateProgramItems(editRows, selected.from_date, selected.to_date)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e)); return
    }
    if (mutationLocked.current) return
    mutationLocked.current = true
    setSaving(true); setError(''); setMessage('')
    try {
      const saved = await updateDbrProgram(selected.id, { items })
      setSelected(saved); setEditRows(programToDraftRows(saved))
      setMessage(`Строки программы №${saved.id} сохранены`)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      mutationLocked.current = false; setSaving(false)
    }
  }

  return {
    programs, selected, editRows, loading, saving, error, message,
    fromDate, toDate, title, company, rows,
    setToDate, setTitle, setCompany, changeFromDate,
    load, openProgram, patchRow, addRow, removeRow, submit, approve,
    patchEditRow, addEditRow, removeEditRow, saveDraftItems,
  }
}
