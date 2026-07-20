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

export async function api<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
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
    throw new ApiError(message, res.status, detail)
  }

  return res.json()
}
