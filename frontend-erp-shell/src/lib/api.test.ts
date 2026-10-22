import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, apiText, onApiUnauthorized, setApiAccessTokenProvider } from './api'

describe('api transport', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    setApiAccessTokenProvider(null)
  })

  it('returns a successful JSON response', async () => {
    const payload = { id: 42, status: 'posted' }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(api<typeof payload>('/ledger/postings/42')).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/ledger/postings/42',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    )
  })

  it('returns undefined for a successful 204 response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })))

    await expect(api<void>('/session', { method: 'DELETE' })).resolves.toBeUndefined()
  })

  it('returns text through the shared transport', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response('<html>Маршрутный лист</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      }),
    ))

    await expect(apiText('/print')).resolves.toContain('Маршрутный лист')
  })

  it('preserves structured error detail in ApiError', async () => {
    const detail = {
      message: 'Проводка не сбалансирована',
      deficit_lines: [{ account: 'materials', amount: 125 }],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    const request = api('/ledger/postings')

    await expect(request).rejects.toMatchObject({
      name: 'ApiError',
      message: detail.message,
      status: 409,
      detail,
    } satisfies Partial<ApiError>)
  })

  it('renders a concise stale-truth message and preserves badge metadata', async () => {
    const detail = {
      code: 'planning_truth_unavailable',
      truth_status: 'stale',
      ledger_generation: 2131,
      cutoff: '2026-10-13T09:40:00+03:00',
      reason: 'Accepted generation exceeded freshness threshold',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(api('/production-control')).rejects.toMatchObject({
      message: 'Данные об остатках устарели. Действия, зависящие от остатков, временно недоступны.',
      status: 503,
      detail,
    } satisfies Partial<ApiError>)
  })

  it.each(['production_current_revision_stale', 'production_current_locator_stale'])(
    'renders a readable production snapshot conflict for %s', async (code) => {
      const detail = { code, expected_source_revision: 'rev-8' }
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
        JSON.stringify({ detail }), { status: 409 },
      )))
      await expect(api('/production-control')).rejects.toMatchObject({
        message: 'Журнал производства изменился. Обновите журнал и выберите строку повторно.',
        status: 409,
        detail,
      } satisfies Partial<ApiError>)
    },
  )

  it('renders a concise unavailable-truth message', async () => {
    const detail = {
      code: 'planning_truth_unavailable',
      truth_status: 'uninitialized',
      ledger_generation: null,
      cutoff: null,
      reason: 'No accepted generation',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(api('/production-control')).rejects.toMatchObject({
      message: 'Актуальные данные об остатках недоступны. Действия, зависящие от остатков, временно недоступны.',
      status: 503,
      detail,
    } satisfies Partial<ApiError>)
  })

  it('renders a concise material-custody unavailable message and preserves detail', async () => {
    const detail = {
      code: 'material_custody_snapshot_unavailable',
      status: 'unavailable',
      expected_generation_id: 2131,
      stored_generation_id: 2130,
      reason: 'compact current custody has an unpublished event tail',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(api('/release-feasibility')).rejects.toMatchObject({
      message: 'Не удалось получить актуальные данные о движении материалов.',
      status: 503,
      detail,
    } satisfies Partial<ApiError>)
  })

  it('notifies unauthorized listeners for a 401 response', async () => {
    const unauthorized = vi.fn()
    const unsubscribe = onApiUnauthorized(unauthorized)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'Сессия истекла' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    try {
      await expect(api('/protected')).rejects.toMatchObject({ status: 401 })
      expect(unauthorized).toHaveBeenCalledOnce()
    } finally {
      unsubscribe()
    }
  })

  it('does not notify unauthorized listeners for a 403 response', async () => {
    const unauthorized = vi.fn()
    const unsubscribe = onApiUnauthorized(unauthorized)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'Недостаточно прав' }), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    try {
      await expect(api('/admin-only')).rejects.toMatchObject({ status: 403 })
      expect(unauthorized).not.toHaveBeenCalled()
    } finally {
      unsubscribe()
    }
  })

  it('still throws the ApiError when an unauthorized listener fails', async () => {
    const unsubscribe = onApiUnauthorized(() => { throw new Error('listener failure') })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('unauthorized', { status: 401 })))
    try {
      await expect(api('/protected')).rejects.toMatchObject({ status: 401 })
    } finally {
      unsubscribe()
    }
  })
})
