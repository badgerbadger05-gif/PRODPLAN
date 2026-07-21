import type { DbrProgram, DbrProgramCreate, DbrProgramItemIn } from '../../../domain/dbr'
import type { PickedItem } from '../../dbr/ItemPicker'

export type DraftProgramRow = {
  key: string
  item: PickedItem | null
  program_date: string
  dateEdited: boolean
  qty: string
  comment: string
}

export function createDraftProgramRow(key: string, dateDefault: string): DraftProgramRow {
  return { key, item: null, program_date: dateDefault, dateEdited: false, qty: '', comment: '' }
}

export function programToDraftRows(program: DbrProgram): DraftProgramRow[] {
  return program.items.map((row) => ({
    key: `saved-${row.id}`,
    item: {
      item_id: row.item_id,
      item_code: row.item_code || String(row.item_id),
      item_name: row.item_name || row.item_code || `ID ${row.item_id}`,
    },
    program_date: row.program_date,
    dateEdited: true,
    qty: String(row.qty),
    comment: row.comment || '',
  }))
}

export function alignFirstDraftDate(rows: DraftProgramRow[], nextDate: string): DraftProgramRow[] {
  return rows.map((row, index) => (
    index === 0 && !row.dateEdited ? { ...row, program_date: nextDate } : row
  ))
}

export function validateProgramItems(
  rows: DraftProgramRow[],
  fromDate: string,
  toDate: string,
): DbrProgramItemIn[] {
  if (!fromDate || !toDate || fromDate > toDate) throw new Error('Проверьте период программы')
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
    if (seen.has(key)) throw new Error(`Номенклатура ${row.item.item_code} повторяется на дату ${row.program_date}`)
    seen.add(key)
    return {
      item_id: row.item.item_id,
      program_date: row.program_date,
      qty: quantity,
      comment: row.comment.trim() || null,
    }
  })
}

export function buildProgramCreatePayload(
  rows: DraftProgramRow[],
  fromDate: string,
  toDate: string,
  title: string,
  company: string,
): DbrProgramCreate {
  return {
    from_date: fromDate,
    to_date: toDate,
    title: title.trim() || null,
    company: company.trim() || null,
    items: validateProgramItems(rows, fromDate, toDate),
  }
}

export function programStatusLabel(status: string): string {
  if (status === 'approved') return 'Утверждена'
  if (status === 'draft') return 'Черновик'
  return status
}
