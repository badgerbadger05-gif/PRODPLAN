import { afterEach, describe, expect, it, vi } from 'vitest'
import { listAssemblyQueue } from './assemblyQueue'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('listAssemblyQueue', () => {
  it('reads the persisted canonical assembly queue endpoint', async () => {
    const payload = {
      rows: [],
      total_rows: 0,
      total_queue_qty: 0,
      truth_meta: {
        ledger_generation: 42,
        cutoff: '2026-07-26T00:00:00+00:00',
        truth_status: 'accepted',
        truth_reason: null,
      },
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(listAssemblyQueue()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/production-control/assembly-queue',
      expect.objectContaining({
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
})
