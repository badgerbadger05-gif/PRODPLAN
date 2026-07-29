import { afterEach, describe, expect, it, vi } from 'vitest'
import { closePaintWeldChain, printRouteSheets } from './productionControl'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('printRouteSheets', () => {
  it('returns the HTML document body instead of trying to parse JSON', async () => {
    const html = '<!doctype html><html><body>Маршрутный лист</body></html>'
    const jsonSpy = vi.fn(async () => {
      throw new SyntaxError('Unexpected token < in JSON')
    })
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: jsonSpy,
      text: async () => html,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(printRouteSheets([1, 2])).resolves.toBe(html)
    expect(jsonSpy).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/production-control/route-sheets/print',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})

describe('closePaintWeldChain', () => {
  it('posts the router payload of /v1/paint-weld/chain/close', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'ok', chain_state: 'closed', resume_required: false }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await closePaintWeldChain(1234)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/paint-weld/chain/close')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      product_id: 1234,
      dry_run: false,
      allow_production: true,
      initiated_by: 'erp-shell-chain-close',
    })
  })

  it('returns a resumable partial close as a 200 answer, not an error', async () => {
    // Частичное закрытие приходит с HTTP 200: проведённые документы не
    // откатываются, UI обязан показать message и предложить докат.
    const payload = {
      status: 'partial',
      dry_run: false,
      chain_link_id: 7,
      chain_state: 'partially_posted',
      resume_required: true,
      posted_sides: ['weld'],
      pending_sides: ['paint'],
      message: 'Цепочка частично проведена: требуется докат.',
      error: 'paint: 1С не создала и не провела СборкаЗапасов',
      weld: { product_id: 1, order_id: 11, remaining_qty: 0, qty_to_produce: 5 },
      paint: { product_id: 2, order_id: 12, remaining_qty: 5, qty_to_produce: 5 },
    }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => payload })
    vi.stubGlobal('fetch', fetchMock)

    const result = await closePaintWeldChain(1)

    expect(result.status).toBe('partial')
    expect(result.resume_required).toBe(true)
    expect(result.chain_state).toBe('partially_posted')
    expect(result.pending_sides).toEqual(['paint'])
    expect(result.message).toContain('докат')
  })
})
