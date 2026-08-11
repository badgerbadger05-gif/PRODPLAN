import { describe, expect, it } from 'vitest'

import { qty } from './format'

describe('qty', () => {
  it.each([null, undefined, '', 'not-a-number', Number.NaN, Number.POSITIVE_INFINITY])(
    'renders unavailable value %s as a dash',
    (value) => {
      expect(qty(value)).toBe('—')
    },
  )

  it('preserves real zero and numeric formatting', () => {
    expect(qty(0)).toBe('0')
    expect(qty('0')).toBe('0')
    expect(qty(12.3456)).toBe('12,346')
    expect(qty(-2)).toBe('-2')
  })
})
