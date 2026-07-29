import { afterEach, describe, expect, it, vi } from 'vitest'
import { searchNomenclature } from './nomenclature'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('searchNomenclature', () => {
  it('reads the canonical nomenclature search and returns item_id for the plan row', async () => {
    const payload = {
      items: [
        { item_id: 42, item_code: '000042', item_name: 'Рама', item_article: 'РМ-1', similarity: 1 },
      ],
      total: 1,
      query: 'рам',
      search_type: 'text',
    }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => payload })
    vi.stubGlobal('fetch', fetchMock)

    await expect(searchNomenclature('рам')).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit | undefined]
    expect(url).toBe('/api/v1/nomenclature/search?q=%D1%80%D0%B0%D0%BC&limit=12')
    // Поиск — единственный вызов при добавлении строки плана: никакого
    // POST /v1/plan/ensure_item, который раньше переписывал справочник.
    expect(init?.method ?? 'GET').toBe('GET')
  })

  it('never writes to the nomenclature directory', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 0, query: 'xx', search_type: 'text' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await searchNomenclature('xx', 5)

    const calls = fetchMock.mock.calls as [string, RequestInit | undefined][]
    expect(calls).toHaveLength(1)
    expect(calls[0][0]).toContain('limit=5')
    expect(calls.every(([, init]) => (init?.method ?? 'GET') === 'GET')).toBe(true)
  })
})
