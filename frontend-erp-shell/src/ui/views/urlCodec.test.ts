import { describe, expect, it } from 'vitest'
import type { ViewState } from './types'
import {
  decodeViewState,
  encodeViewState,
  isViewState,
  VIEW_STATE_URL_VERSION,
} from './urlCodec'

const state: ViewState = {
  filters: {
    search: 'редуктор № 42',
    urgent: true,
    warehouseIds: ['main', 'резерв'],
    quantities: [1, 2.5],
    empty: null,
  },
  sort: [
    { field: 'required_date', direction: 'asc' },
    { field: 'name', direction: 'desc' },
  ],
  visibleColumns: ['name', 'required_date'],
  density: 'compact',
}

function rawEnvelope(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/u, '')
}

describe('view state URL codec', () => {
  it('round-trips a complete state with unicode through a URL-safe value', () => {
    const encoded = encodeViewState(state)

    expect(encoded).toMatch(/^[A-Za-z0-9_-]+$/u)
    expect(decodeViewState(encoded)).toEqual(state)
  })

  it('includes a version and rejects unsupported versions', () => {
    expect(VIEW_STATE_URL_VERSION).toBe(1)
    expect(decodeViewState(rawEnvelope({ v: 2, state }))).toBeNull()
  })

  it.each([
    null,
    undefined,
    '',
    'not+base64',
    rawEnvelope({ v: 1 }),
    rawEnvelope({ v: 1, state: { ...state, density: 'tiny' } }),
    rawEnvelope({ v: 1, state: { ...state, sort: [{ field: 'name', direction: 'sideways' }] } }),
    rawEnvelope({ v: 1, state: { ...state, visibleColumns: ['name', 42] } }),
    rawEnvelope({ v: 1, state: { ...state, filters: { bad: ['valid', 42] } } }),
    rawEnvelope({ v: 1, state, unexpected: true }),
  ])('gracefully rejects absent, corrupt or invalid input %#', (encoded) => {
    expect(decodeViewState(encoded)).toBeNull()
  })

  it('exposes the same runtime validation used by both codec directions', () => {
    expect(isViewState(state)).toBe(true)
    expect(isViewState({ ...state, filters: { quantity: Number.POSITIVE_INFINITY } })).toBe(false)
    expect(() => encodeViewState({ ...state, density: 'tiny' } as unknown as ViewState))
      .toThrow('Invalid view state')
  })
})
