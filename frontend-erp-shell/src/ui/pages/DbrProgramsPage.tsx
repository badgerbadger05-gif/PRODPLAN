import { useCallback, useEffect, useState } from 'react'
import type { DbrProgram, DbrProgramItemIn } from '../../domain/dbr'
import { dateRu, isoToday, qty, shiftIsoDate } from '../../lib/format'
import {
  approveDbrProgram,
  createDbrProgram,
  getDbrProgram,
  listDbrPrograms,
  updateDbrProgram,
} from '../../services/dbr'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { DbrNav } from '../dbr/DbrNav'
import { ItemPicker } from '../dbr/ItemPicker'
import type { PickedItem } from '../dbr/ItemPicker'

type DraftRow = {
  key: string
  item: PickedItem | null
  program_date: string
  qty: string
  comment: string
}

let rowSeq = 0
function newRow(dateDefault: string): DraftRow {
  rowSeq += 1
  return { key: `r${rowSeq}`, item: null, program_date: dateDefault, qty: '', comment: '' }
}

function statusLabel(status: string) {
  if (status === 'approved') return 'Утверждена'
  if (status === 'draft') return 'Черновик'
  return status
}

function programRows(program: DbrProgram): DraftRow[] {
  return program.items.map((row) => ({
    key: `saved-${row.id}`,
    item: {
      item_id: row.item_id,
      item_code: row.item_code || String(row.item_id),
      item_name: row.item_name || row.item_code || `ID ${row.item_id}`,
    },
    program_date: row.program_date,
    qty: String(row.qty),
    comment: row.comment || '',
  }))
}

function validatedItems(rows: DraftRow[], fromDate: string, toDate: string): DbrProgramItemIn[] {
  if (!fromDate || !toDate || fromDate > toDate) {
    throw new Error('Проверьте период программы')
  }
  if (!rows.length) throw new Error('Добавьте хотя бы одну строку программы')
  const seen = new Set<string>()
  return rows.map((row) => {
    const quantity = Number(row.qty)
    if (!row.item || !row.program_date || !Number.isFinite(quantity) || quantity <= 0) {
      throw new Error('В каждой строке укажите номенклатуру, дату и количество больше нуля')
    }
    if (row.program_date < fromDate || row.program_date > toDate) {
      throw new Error(`Дата ${row.program_date} находится вне периода программы`)
    }
    const key = `${row.item.item_id}:${row.program_date}`
    if (seen.has(key)) {
      throw new Error(`Номенклатура ${row.item.item_code} повторяется на дату ${row.program_date}`)
    }
    seen.add(key)
    return {
      item_id: row.item.item_id,
      program_date: row.program_date,
      qty: quantity,
      comment: row.comment.trim() || null,
    }
  })
}

export function DbrProgramsPage() {
  const [programs, setPrograms] = useState<DbrProgram[]>([])
  const [selected, setSelected] = useState<DbrProgram | null>(null)
  const [editRows, setEditRows] = useState<DraftRow[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  // Creation form state.
  const [fromDate, setFromDate] = useState(isoToday())
  const [toDate, setToDate] = useState(shiftIsoDate(isoToday(), 14))
  const [title, setTitle] = useState('')
  const [company, setCompany] = useState('')
  const [rows, setRows] = useState<DraftRow[]>(() => [newRow(isoToday())])

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

  useEffect(() => {
    void load()
  }, [load])

  async function openProgram(id: number) {
    setError('')
    setMessage('')
    try {
      const program = await getDbrProgram(id)
      setSelected(program)
      setEditRows(programRows(program))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function patchRow(key: string, next: Partial<DraftRow>) {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...next } : r)))
  }

  function addRow() {
    setRows((prev) => [...prev, newRow(fromDate)])
  }

  function removeRow(key: string) {
    setRows((prev) => (prev.length > 1 ? prev.filter((r) => r.key !== key) : prev))
  }

  async function submit() {
    let items: DbrProgramItemIn[]
    try {
      items = validatedItems(rows, fromDate, toDate)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const created = await createDbrProgram({
        from_date: fromDate,
        to_date: toDate,
        title: title.trim() || null,
        company: company.trim() || null,
        items,
      })
      setMessage(`Программа №${created.id} создана (${items.length} строк)`)
      setSelected(created)
      setEditRows(programRows(created))
      setRows([newRow(fromDate)])
      setTitle('')
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function approve(id: number) {
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const approved = await approveDbrProgram(id)
      setMessage(`Программа №${id} утверждена`)
      setSelected(approved)
      setEditRows(programRows(approved))
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function patchEditRow(key: string, next: Partial<DraftRow>) {
    setEditRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...next } : row)))
  }

  async function saveDraftItems() {
    if (!selected || selected.status !== 'draft') return
    let items: DbrProgramItemIn[]
    try {
      items = validatedItems(editRows, selected.from_date, selected.to_date)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const saved = await updateDbrProgram(selected.id, { items })
      setSelected(saved)
      setEditRows(programRows(saved))
      setMessage(`Строки программы №${saved.id} сохранены`)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование DBR / Производственные программы</div>
        <div className="runBadge">Программ: {programs.length}</div>
      </div>

      <DocumentWindow
        title="Производственные программы"
        subtitle="План выпуска по датам — источник для построения барабана"
        hotkeys="F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={programs.length ? 1 : 0}
            visibleTo={programs.length}
            total={programs.length}
            selectedCount={selected ? 1 : 0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <DbrNav />

        <div className="commandBar">
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="dbrScroll">
          {/* ── Create program ─────────────────────────────────────────── */}
          <section className="dbrSection">
            <h2>Новая программа</h2>
            <div className="dbrProgramHead">
              <label className="dbrField">
                <span>Период с</span>
                <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} />
              </label>
              <label className="dbrField">
                <span>Период по</span>
                <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} />
              </label>
              <label className="dbrField">
                <span>Название</span>
                <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="напр. Июль 2026" />
              </label>
              <label className="dbrField">
                <span>Компания</span>
                <input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="необязательно" />
              </label>
            </div>

            <table className="journalTable dbrTable dbrProgramTable">
              <thead>
                <tr>
                  <th className="itemCell">Номенклатура</th>
                  <th className="dateCol">Дата</th>
                  <th className="numCell">Кол-во</th>
                  <th className="itemCell">Комментарий</th>
                  <th className="dbrActionCol"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.key}>
                    <td className="itemCell">
                      <ItemPicker value={row.item} onChange={(item) => patchRow(row.key, { item })} />
                    </td>
                    <td className="dateCol">
                      <input
                        type="date"
                        value={row.program_date}
                        onChange={(e) => patchRow(row.key, { program_date: e.target.value })}
                      />
                    </td>
                    <td className="numCell">
                      <input
                        type="number"
                        step="0.001"
                        min="0.001"
                        value={row.qty}
                        placeholder="шт"
                        onChange={(e) => patchRow(row.key, { qty: e.target.value })}
                      />
                    </td>
                    <td className="itemCell">
                      <input
                        value={row.comment}
                        onChange={(e) => patchRow(row.key, { comment: e.target.value })}
                        placeholder="необязательно"
                      />
                    </td>
                    <td className="dbrActionCol">
                      <button onClick={() => removeRow(row.key)} disabled={rows.length <= 1}>Удалить</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="commandBar">
              <button onClick={addRow} disabled={saving}>Добавить строку</button>
              <div className="commandBarSpacer" />
              <button className="primary" onClick={() => void submit()} disabled={saving}>Создать программу</button>
            </div>
          </section>

          {/* ── Program list ───────────────────────────────────────────── */}
          <section className="dbrSection">
            <h2>Список программ</h2>
            <table className="journalTable dbrTable">
              <thead>
                <tr>
                  <th className="numCell">№</th>
                  <th className="itemCell">Название</th>
                  <th className="dateCol">Период</th>
                  <th className="numCell">Строк</th>
                  <th className="itemCell">Статус</th>
                  <th className="dbrActionCol"></th>
                </tr>
              </thead>
              <tbody>
                {programs.map((program) => (
                  <tr
                    key={program.id}
                    className={selected?.id === program.id ? 'selectedRow' : ''}
                    onClick={() => void openProgram(program.id)}
                  >
                    <td className="numCell"><strong>{program.id}</strong></td>
                    <td className="itemCell">
                      <strong>{program.title || '(без названия)'}</strong>
                      <span>{program.company || ''}</span>
                    </td>
                    <td className="dateCol">{dateRu(program.from_date)} — {dateRu(program.to_date)}</td>
                    <td className="numCell">{program.items.length}</td>
                    <td className="itemCell">
                      <span className={`miniPill ${program.status === 'approved' ? 'ready' : ''}`}>
                        {statusLabel(program.status)}
                      </span>
                    </td>
                    <td className="dbrActionCol">
                      {program.status === 'draft' && (
                        <button
                          className="primary"
                          onClick={(e) => { e.stopPropagation(); void approve(program.id) }}
                          disabled={saving}
                        >
                          Утвердить
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {!programs.length && (
                  <tr><td colSpan={6} className="emptyDetail">{loading ? 'Загрузка…' : 'Программы не созданы'}</td></tr>
                )}
              </tbody>
            </table>
          </section>

          {/* ── Selected program detail ────────────────────────────────── */}
          {selected && (
            <section className="dbrSection">
              <div className="dbrSectionHead">
                <h2>Программа №{selected.id}: {selected.title || '(без названия)'}</h2>
                <div className="commandBar">
                  {selected.status === 'draft' && (
                    <>
                      <button
                        onClick={() => setEditRows((prev) => [...prev, newRow(selected.from_date)])}
                        disabled={saving}
                      >
                        Добавить строку
                      </button>
                      <button className="primary" onClick={() => void saveDraftItems()} disabled={saving}>Сохранить строки</button>
                      <button onClick={() => void approve(selected.id)} disabled={saving}>Утвердить</button>
                    </>
                  )}
                </div>
              </div>
              <table className="journalTable dbrTable">
                <thead>
                  <tr>
                    <th className="dateCol">Дата</th>
                    <th className="itemCell">Номенклатура</th>
                    <th className="numCell">Кол-во</th>
                    <th className="itemCell">Комментарий</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.status === 'draft' ? editRows.map((row) => (
                    <tr key={row.key}>
                      <td className="dateCol">
                        <input type="date" value={row.program_date} onChange={(e) => patchEditRow(row.key, { program_date: e.target.value })} />
                      </td>
                      <td className="itemCell">
                        <ItemPicker value={row.item} onChange={(item) => patchEditRow(row.key, { item })} />
                      </td>
                      <td className="numCell">
                        <input type="number" min="0.001" step="0.001" value={row.qty} onChange={(e) => patchEditRow(row.key, { qty: e.target.value })} />
                      </td>
                      <td className="itemCell">
                        <input value={row.comment} onChange={(e) => patchEditRow(row.key, { comment: e.target.value })} />
                        <button onClick={() => setEditRows((prev) => prev.filter((item) => item.key !== row.key))} disabled={saving}>Удалить</button>
                      </td>
                    </tr>
                  )) : selected.items.map((item) => (
                    <tr key={item.id}>
                      <td className="dateCol">{dateRu(item.program_date)}</td>
                      <td className="itemCell">
                        <strong>{item.item_name || item.item_code || `ID ${item.item_id}`}</strong>
                        <span>{item.item_code || `ID ${item.item_id}`}</span>
                      </td>
                      <td className="numCell"><strong>{qty(item.qty)}</strong></td>
                      <td className="itemCell">{item.comment || ''}</td>
                    </tr>
                  ))}
                  {!(selected.status === 'draft' ? editRows : selected.items).length && (
                    <tr><td colSpan={4} className="emptyDetail">В программе нет строк</td></tr>
                  )}
                </tbody>
              </table>
            </section>
          )}
        </div>
      </DocumentWindow>
    </main>
  )
}
