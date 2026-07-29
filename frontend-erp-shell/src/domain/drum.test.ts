import { describe, expect, it } from 'vitest'
import { drumItemLabel } from './drum'

describe('drumItemLabel', () => {
  it('renders "код — имя" when the backend sends both', () => {
    expect(drumItemLabel({ item_id: 5, item_code: 'НФ-000123', item_name: 'Кронштейн' })).toBe(
      'НФ-000123 — Кронштейн',
    )
  })

  it('falls back to whichever identity field is present', () => {
    expect(drumItemLabel({ item_id: 5, item_code: 'НФ-000123' })).toBe('НФ-000123')
    expect(drumItemLabel({ item_id: 5, item_name: 'Кронштейн' })).toBe('Кронштейн')
  })

  it('keeps the page readable on a backend that still omits the fields', () => {
    expect(drumItemLabel({ item_id: 5 })).toBe('5')
    expect(drumItemLabel({ item_id: 5, item_code: '  ', item_name: null })).toBe('5')
  })
})
