import { afterEach, describe, expect, it, vi } from 'vitest'
import { getNomenclatureGroupSelection, listNomenclatureGroups } from './sync'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('listNomenclatureGroups', () => {
  it('reads the real 1C contract: {value: [{Ref_Key, Code, Description, IsFolder}]}', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        value: [
          { Ref_Key: 'ref-1', Code: '000001', Description: 'Метизы', IsFolder: true },
          { Ref_Key: 'ref-2', Code: '000002', Description: 'Прокат', IsFolder: true },
        ],
      }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(listNomenclatureGroups()).resolves.toEqual([
      { id: 'ref-1', code: '000001', name: 'Метизы' },
      { id: 'ref-2', code: '000002', name: 'Прокат' },
    ])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/odata/groups', expect.anything())
  })

  it('drops non-folder rows and rows without a Ref_Key', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        value: [
          { Ref_Key: 'ref-1', Code: '1', Description: 'Группа', IsFolder: true },
          { Ref_Key: 'ref-2', Code: '2', Description: 'Позиция', IsFolder: false },
          { Ref_Key: '', Code: '3', Description: 'Мусор', IsFolder: true },
        ],
      }),
    }))

    await expect(listNomenclatureGroups()).resolves.toEqual([
      { id: 'ref-1', code: '1', name: 'Группа' },
    ])
  })

  it('returns an empty list when the cache file is missing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ value: [] }) }))
    await expect(listNomenclatureGroups()).resolves.toEqual([])
  })
})

describe('getNomenclatureGroupSelection', () => {
  it('reads the saved selection from its own endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ids: ['ref-1', 'ref-2'] }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(getNomenclatureGroupSelection()).resolves.toEqual(['ref-1', 'ref-2'])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/odata/groups/selection', expect.anything())
  })
})
