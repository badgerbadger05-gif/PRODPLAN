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
      limit: 1000,
      offset: 0,
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

  it('passes the requested page window to the backend', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ rows: [], total_rows: 0, total_queue_qty: 0, limit: 50, offset: 50 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await listAssemblyQueue({ limit: 50, offset: 50 })

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/production-control/assembly-queue?limit=50&offset=50',
    )
  })
})
