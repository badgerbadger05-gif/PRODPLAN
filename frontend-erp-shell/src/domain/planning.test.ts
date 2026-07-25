import { describe, expect, it } from 'vitest'
import { executionFlowSummary } from './planning'

describe('executionFlowSummary', () => {
  it('keeps a label for every flow and orders known flows first', () => {
    const rows = executionFlowSummary({
      production: { completed_qty: 197, base_qty: 200, execution_pct: 98.5, available: true },
      purchase: { completed_qty: 0, base_qty: 10, execution_pct: 0, available: true },
      rework: { completed_qty: 6, base_qty: 10, execution_pct: 59.6, available: true },
    })
    expect(rows).toEqual([
      { flow: 'purchase', label: 'Закупка', text: '0%' },
      { flow: 'production', label: 'Производство', text: '98.5%' },
      { flow: 'rework', label: 'Переработка', text: '59.6%' },
    ])
  })

  it('renders an unavailable flow as "недоступно" WITH its label', () => {
    const rows = executionFlowSummary({
      purchase: { completed_qty: 0, base_qty: 0, execution_pct: null, available: false },
      production: { completed_qty: 197, base_qty: 200, execution_pct: 98.5, available: true },
    })
    expect(rows).toEqual([
      { flow: 'purchase', label: 'Закупка', text: 'недоступно' },
      { flow: 'production', label: 'Производство', text: '98.5%' },
    ])
  })

  it('treats a null percentage as "недоступно" even without the available flag', () => {
    const rows = executionFlowSummary({
      rework: { completed_qty: 0, base_qty: 5, execution_pct: null },
    })
    expect(rows).toEqual([{ flow: 'rework', label: 'Переработка', text: 'недоступно' }])
  })

  it('shows the raw key for an unknown flow and appends it after known flows', () => {
    const rows = executionFlowSummary({
      logistics: { completed_qty: 1, base_qty: 4, execution_pct: 25, available: true },
      production: { completed_qty: 5, base_qty: 5, execution_pct: 100, available: true },
    })
    expect(rows).toEqual([
      { flow: 'production', label: 'Производство', text: '100%' },
      { flow: 'logistics', label: 'logistics', text: '25%' },
    ])
  })

  it('skips available net-zero flows but never a null / absent map', () => {
    expect(executionFlowSummary(undefined)).toEqual([])
    expect(executionFlowSummary(null)).toEqual([])
    expect(
      executionFlowSummary({
        purchase: { completed_qty: 0, base_qty: 0, execution_pct: 100, available: true },
      }),
    ).toEqual([])
  })
})
