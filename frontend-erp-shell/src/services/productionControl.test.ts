import { afterEach, describe, expect, it, vi } from 'vitest'
import { printRouteSheets } from './productionControl'

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
