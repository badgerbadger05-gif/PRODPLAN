// Error thrown for any non-2xx response. Carries the HTTP status and the parsed
// `detail` payload so callers can render structured 409 bodies (e.g. the DBR
// launch conflict with its `deficit_lines`) instead of a stringified blob.
export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type UnauthorizedListener = () => void
type AccessTokenProvider = () => string | null | undefined

const unauthorizedListeners = new Set<UnauthorizedListener>()
let accessTokenProvider: AccessTokenProvider | null = null

export function setApiAccessTokenProvider(provider: AccessTokenProvider | null) {
  accessTokenProvider = provider
}

export function onApiUnauthorized(listener: UnauthorizedListener) {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

function notifyUnauthorized() {
  unauthorizedListeners.forEach((listener) => {
    try {
      listener()
    } catch {
      // A UI subscriber must never swallow the original HTTP error.
    }
  })
}

async function request<T>(
  path: string,
  init: RequestInit | undefined,
  signal: AbortSignal | undefined,
  responseMode: 'auto' | 'text',
): Promise<T> {
  const token = accessTokenProvider?.()
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
    signal: signal ?? init?.signal,
  })

  if (!res.ok) {
    const text = await res.text()
    let message = text || `HTTP ${res.status}`
    let detail: unknown
    try {
      const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown; error?: unknown }
      detail = parsed.detail ?? parsed.message ?? parsed.error
      if (typeof detail === 'string') {
        message = detail
      } else if (detail && typeof detail === 'object') {
        // Structured detail (e.g. 409 {message, deficit_lines}) — prefer its
        // human `message` field for the thrown Error, keep the object on detail.
        const inner = (detail as Record<string, unknown>).message
        message = typeof inner === 'string' ? inner : JSON.stringify(detail)
      }
    } catch {
      // Keep the original response text when it is not JSON.
    }
    if (res.status === 401) notifyUnauthorized()
    throw new ApiError(message, res.status, detail)
  }

  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  if (responseMode === 'text') return text as T
  const contentType = res.headers.get('content-type') ?? ''
  return (contentType.includes('json') ? JSON.parse(text) : text) as T
}

export function api<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  return request<T>(path, init, signal, 'auto')
}

export function apiText(path: string, init?: RequestInit, signal?: AbortSignal): Promise<string> {
  return request<string>(path, init, signal, 'text')
}
