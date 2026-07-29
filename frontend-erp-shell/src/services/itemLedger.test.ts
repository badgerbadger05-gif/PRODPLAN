import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  getItemLedgerMovements,
  getItemLedgerPosition,
  getItemLedgerReservations,
  getItemLedgerReservationEvents,
} from './itemLedger'

function mockFetchJson(payload: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function expectGetOnlyFetchCall(fetchMock: ReturnType<typeof vi.fn>) {
  const [url, init] = fetchMock.mock.calls[0] as [
    string,
    (RequestInit & { body?: string | URLSearchParams | null }) | undefined,
  ]
  expect(url).toContain('/api/')
  expect(init?.method).toBeUndefined()
  expect(init?.body).toBeUndefined()
  expect(init?.headers).toBeTruthy()
}

describe('item-ledger boundary service', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('loads position with exact GET URL', async () => {
    const payload = {
      item_id: 9401,
      item_code: '00000063',
      item_name: 'Труба',
      pool_key: '9401::default',
      on_hand: 335.144,
      on_hand_by_warehouse: [],
      incoming_supplier: 120,
      incoming_wip: 0,
      incoming: 120,
      reserved_soft: 526.2,
      available: -191.06,
      projected: -71.06,
      uncovered: 71.06,
      flags: { on_hand_negative: false, has_uncovered: true, reconcile_pending: false },
    }
    const fetchMock = mockFetchJson(payload)

    await expect(getItemLedgerPosition(9401)).resolves.toEqual(payload)

    const [url, init] = fetchMock.mock.calls[0] as [
      string,
      (RequestInit & { body?: string | URLSearchParams | null }) | undefined,
    ]
    expect(url).toBe('/api/v1/item-ledger/9401/position')
    expect(init?.method).toBeUndefined()
    expect(init?.body).toBeUndefined()
  })

  it('loads movements with fully encoded filters/pagination', async () => {
    const payload = { total: 1, limit: 25, offset: 10, rows: [] }
    const fetchMock = mockFetchJson(payload)

    await expect(
      getItemLedgerMovements(15, {
        date_from: '2026-07-01',
        date_to: '2026-07-21',
        warehouse_ref1c: 'WH/S&A',
        limit: 25,
        offset: 10,
      }),
    ).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/item-ledger/15/movements?date_from=2026-07-01&date_to=2026-07-21&warehouse_ref1c=WH%2FS%26A&limit=25&offset=10',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    )
    expectGetOnlyFetchCall(fetchMock)
  })

  it('loads reservations with encoded status filter and run_id', async () => {
    const payload = { rows: [] }
    const fetchMock = mockFetchJson(payload)

    await expect(
      getItemLedgerReservations(15, { status: 'active|closed', run_id: 17 }),
    ).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/item-ledger/15/reservations?status=active%7Cclosed&run_id=17',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    )
    expectGetOnlyFetchCall(fetchMock)
  })

  it('loads reservation events with exact path', async () => {
    const payload = { reservation_id: 5123, rows: [] }
    const fetchMock = mockFetchJson(payload)

    await expect(getItemLedgerReservationEvents(15, 5123)).resolves.toEqual(payload)

    const [url, init] = fetchMock.mock.calls[0] as [
      string,
      (RequestInit & { body?: string | URLSearchParams | null }) | undefined,
    ]
    expect(url).toBe('/api/v1/item-ledger/15/reservations/5123/events')
    expect(init?.method).toBeUndefined()
    expect(init?.body).toBeUndefined()
  })

})
