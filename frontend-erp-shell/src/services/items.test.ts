import { afterEach, describe, expect, it, vi } from 'vitest'
import { updateItemOptimalBatch } from './items'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('updateItemOptimalBatch', () => {
  it('patches only the edited attribute and never reads or resends stock_qty', async () => {
    const card = {
      item_id: 7,
      item_code: '000001',
      item_name: 'Кронштейн',
      stock_qty: 17.5,
      optimal_batch: 24,
      status: 'active',
    }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => card })
    vi.stubGlobal('fetch', fetchMock)

    await expect(updateItemOptimalBatch(7, 24)).resolves.toEqual(card)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/items/7')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(String(init.body))).toEqual({ optimal_batch: 24 })
  })

  it('sends an explicit null to clear the optimal batch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ item_id: 7, optimal_batch: null }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await updateItemOptimalBatch(7, null)

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ optimal_batch: null })
  })
})
