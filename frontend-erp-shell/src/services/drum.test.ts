import { afterEach, describe, expect, it, vi } from 'vitest'
import { listDrum } from './drum'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('listDrum', () => {
  it('reads the persisted canonical drum endpoint', async () => {
    const payload = {
      schedule_from: '2026-07-26T00:00:00+00:00',
      schedule_to: '2026-08-10T00:00:00+00:00',
      slots: [],
      gaps: [],
      total_open_qty: 0,
      total_slot_qty: 0,
      total_gap_qty: 0,
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

    await expect(listDrum()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/production-control/drum',
      expect.objectContaining({
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
})
