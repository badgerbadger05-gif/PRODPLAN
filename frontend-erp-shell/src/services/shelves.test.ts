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
          item_code: '000420',
          item_name: 'Втулка',
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
          demand_manifest: [
            {
              need_date: '2026-07-27',
              qty: '30.000',
              priority: ['2026-07-27', 11],
              planning_run_id: 5,
              plan_id: 3,
              plan_line_id: 9,
              drum_slot_id: 77,
              freeze_component_id: 88,
            },
          ],
        },
      ],
      total_rows: 1,
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

    await expect(listShelves()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/production-control/shelves',
      expect.objectContaining({
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  it('passes the requested page window to the backend', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ rows: [], total_rows: 0, limit: 50, offset: 100 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await listShelves({ limit: 50, offset: 100 })

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/production-control/shelves?limit=50&offset=100',
    )
  })
})
