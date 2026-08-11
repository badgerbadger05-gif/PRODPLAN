import { describe, expect, it } from 'vitest'
import {
  journalRowStatus,
  journalRowStatusClass,
  journalRowStatusLabel,
  type ExecutionJournalRow,
} from './planning'


const row = (status?: ExecutionJournalRow['status']) => ({
  status,
  net_qty: 10,
  remaining_qty: 0,
  completed_qty: 10,
  ordered_qty: 10,
})


describe('execution journal status contract', () => {
  it('renders execution_unavailable as unavailable rather than completed', () => {
    const status = journalRowStatus(row('execution_unavailable'))
    expect(status).toBe('execution_unavailable')
    expect(journalRowStatusLabel(status)).toBe('Исполнение недоступно')
    expect(journalRowStatusClass(status)).toBe('unavailable')
  })

  it('fails closed when backend status is absent', () => {
    expect(journalRowStatus(row())).toBe('execution_unavailable')
  })
})
