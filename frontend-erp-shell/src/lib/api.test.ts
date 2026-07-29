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
