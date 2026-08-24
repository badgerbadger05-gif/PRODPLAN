import { afterEach, describe, expect, it, vi } from 'vitest'

import { closeProductionOrder, listProductionOrders, listRootProductOptions, updateItem, updateOrderQuantity } from './productionControl'

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

describe('production-control journal boundary', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('calls the canonical production-control journal endpoint with encoded query', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          rows: [],
          total: 0,
          limit: 50,
          offset: 20,
          latest_run_id: 1,
          truth_meta: {
            ledger_generation: 1,
            cutoff: '2026-07-31T00:00:00Z',
            truth_status: 'ok',
            truth_reason: null,
          },
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const params = new URLSearchParams({ limit: '50', offset: '20' })

    await listProductionOrders(params)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/production-control/orders?limit=50&offset=20')
    const init = fetchMock.mock.calls[0]![1]!
    expect(init.method).toBeUndefined()
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' })
  })

  it('loads root products from a dedicated endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          rows: [{ item_id: 10, item_name: 'Test root', item_article: 'R-10', item_code: 'R10' }],
          total: 1,
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await listRootProductOptions()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/production-control/orders/root-products')
  })
})

describe('production-control close action boundary', () => {
  it('posts close request to production control endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ status: 'ok', orders_closed: 1, orders_error: 0, entries: [] }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await closeProductionOrder(101, { dry_run: false })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/production-control/orders/101/close')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ dry_run: false })
  })
})

describe('production-control launch quantity boundary', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('patches the launch quantity of one production line', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: 'ok',
          product_id: 901,
          order_id: 801,
          previous_quantity: 10,
          quantity: 14,
          remaining_qty: 14,
          launchable_qty: 20,
          material_issues_open: 0,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await updateOrderQuantity(901, 14)

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/production-control/orders/901/quantity')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(String(init.body))).toEqual({ quantity: 14, initiated_by: 'erp-shell' })
    expect(result.quantity).toBe(14)
  })
})
