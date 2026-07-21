import { describe, expect, it } from 'vitest'
import type { DbrProgram } from '../../../domain/dbr'
import {
  alignFirstDraftDate,
  buildProgramCreatePayload,
  createDraftProgramRow,
  programStatusLabel,
  programToDraftRows,
  validateProgramItems,
} from './model'

const picked = { item_id: 77, item_code: 'ITEM-77', item_name: 'Корпус' }

describe('dbr programs model', () => {
  it('creates an untouched row and aligns only that first row with the period start', () => {
    const first = createDraftProgramRow('r1', '2026-07-01')
    const second = { ...createDraftProgramRow('r2', '2026-07-02'), dateEdited: true }
    expect(alignFirstDraftDate([first, second], '2026-07-05')).toEqual([
      { ...first, program_date: '2026-07-05' },
      second,
    ])
    expect(alignFirstDraftDate([{ ...first, dateEdited: true }], '2026-07-05')[0].program_date).toBe('2026-07-01')
  })

  it('normalizes a create payload and item quantities, comments, and optional fields', () => {
    const row = { ...createDraftProgramRow('r1', '2026-07-10'), item: picked, qty: '12.500', comment: '  срочно  ' }
    expect(buildProgramCreatePayload([row], '2026-07-01', '2026-07-31', '  Июль  ', '   ')).toEqual({
      from_date: '2026-07-01',
      to_date: '2026-07-31',
      title: 'Июль',
      company: null,
      items: [{ item_id: 77, program_date: '2026-07-10', qty: 12.5, comment: 'срочно' }],
    })
  })

  it('rejects invalid periods, empty rows, invalid quantities, out-of-range dates, and duplicates', () => {
    const row = { ...createDraftProgramRow('r1', '2026-07-10'), item: picked, qty: '1' }
    expect(() => validateProgramItems([row], '2026-08-01', '2026-07-01')).toThrow('Проверьте период')
    expect(() => validateProgramItems([], '2026-07-01', '2026-07-31')).toThrow('Добавьте хотя бы одну')
    expect(() => validateProgramItems([{ ...row, qty: '0' }], '2026-07-01', '2026-07-31')).toThrow('количество больше нуля')
    expect(() => validateProgramItems([row], '2026-07-11', '2026-07-31')).toThrow('вне периода')
    expect(() => validateProgramItems([row, { ...row, key: 'r2' }], '2026-07-01', '2026-07-31')).toThrow('повторяется')
  })

  it('maps persisted rows into edited drafts with stable saved keys and fallbacks', () => {
    const program: DbrProgram = {
      id: 1, from_date: '2026-07-01', to_date: '2026-07-31', status: 'draft',
      items: [{ id: 5, item_id: 77, program_date: '2026-07-10', qty: 3 }],
    }
    expect(programToDraftRows(program)).toEqual([expect.objectContaining({
      key: 'saved-5', dateEdited: true, qty: '3', comment: '',
      item: { item_id: 77, item_code: '77', item_name: 'ID 77' },
    })])
    expect(programStatusLabel('draft')).toBe('Черновик')
    expect(programStatusLabel('approved')).toBe('Утверждена')
    expect(programStatusLabel('archived')).toBe('archived')
  })
})
