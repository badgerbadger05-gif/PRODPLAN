export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })

  if (!res.ok) {
    const text = await res.text()
    let message = text || `HTTP ${res.status}`
    try {
      const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown; error?: unknown }
      const detail = parsed.detail ?? parsed.message ?? parsed.error
      if (typeof detail === 'string') {
        message = detail
      } else if (detail && typeof detail === 'object') {
        message = JSON.stringify(detail)
      }
    } catch {
      // Keep the original response text when it is not JSON.
    }
    throw new Error(message)
  }

  return res.json()
}
