import { afterEach, describe, expect, it, vi } from 'vitest'

import { updateItem } from './productionControl'

describe('production-control item update boundary', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('patches only the requested planning attribute', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ item_id: 7, optimal_batch: 12 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await updateItem(7, { optimal_batch: 12 })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/items/7')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(String(init.body))).toEqual({ optimal_batch: 12 })
  })
})
