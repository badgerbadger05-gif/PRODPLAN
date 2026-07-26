import { afterEach, describe, expect, it, vi } from 'vitest'
import { listShelves } from './shelves'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('listShelves', () => {
  it('reads the persisted canonical shelves projection endpoint', async () => {
    const payload = {
      rows: [
        {
          policy_id: 17,
          item_id: 420,
          warehouse_ref1c: 'A01',
          protection_until: '2026-07-26',
          target_qty: 120,
          shelf_physical_qty: 90,
          other_stock_qty: 20,
          projected_qty: 90,
          gap_qty: 30,
          transfer_qty: 20,
          unlaunched_mrp_qty: 10,
          pull_qty: 10,
          materialized_qty: 10,
          first_shortage_date: '2026-07-27',
          latest_start_date: '2026-07-25',
        },
      ],
      total_rows: 1,
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

    await expect(listShelves()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/production-control/shelves',
      expect.objectContaining({
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
})
