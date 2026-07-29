import { describe, expect, it } from 'vitest'
import { formatField } from './fieldFormat'

describe('formatField', () => {
  it('uses the shared Russian quantity and date formats', () => {
    expect(formatField(1234.5, 'qty')).toBe('1 234,5')
    expect(formatField('2026-07-20', 'date')).toBe('20.07.2026')
  })

  it('maps status codes through Doctype options', () => {
    expect(formatField('posted', 'status', {
      posted: { label: 'Собрано', tone: 'ok' },
    })).toBe('Собрано')
  })

  it('renders missing values consistently', () => {
    expect(formatField(null, 'text')).toBe('—')
    expect(formatField(undefined, 'number')).toBe('—')
    expect(formatField(false, 'bool')).toBe('—')
  })
})

