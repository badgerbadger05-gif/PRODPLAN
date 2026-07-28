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

// Shape shared by every structured `detail` the backend raises for a blocked
// read: `PlanningTruthUnavailable.as_dict()` and the "snapshot is missing for
// accepted generation" bodies of /drum, /shelves and /assembly-queue. Only the
// fields the UI actually renders are declared; the rest stays on `detail`.
export type ProblemDetail = {
  code?: string
  reason?: string
  message?: string
  consumer?: string
  truth_status?: string
  ledger_generation?: number
}

const TRUTH_STATUS_LABEL: Record<string, string> = {
  uninitialized: 'поколение Ledger не опубликовано',
  building: 'поколение ещё строится',
  rejected: 'поколение не принято целиком',
  stale: 'поколение устарело',
  invalidated: 'поколение аннулировано',
  superseded: 'поколение вытеснено новым',
}

const PROBLEM_CODE_LABEL: Record<string, string> = {
  planning_truth_unavailable: 'нет принятого поколения Item Ledger',
  assembly_queue_unavailable: 'снимок очереди сборки отсутствует для принятого поколения',
  drum_schedule_unavailable: 'расписание барабана отсутствует для принятого поколения',
  shelf_projection_unavailable: 'проекция полок отсутствует для принятого поколения',
}

function text(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed ? trimmed : undefined
}

/** Read the known fields of a structured `detail` object. */
export function problemDetail(detail: unknown): ProblemDetail | null {
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  const raw = detail as Record<string, unknown>
  const generation = raw.ledger_generation
  return {
    code: text(raw.code),
    reason: text(raw.reason),
    message: text(raw.message),
    consumer: text(raw.consumer),
    truth_status: text(raw.truth_status),
    ledger_generation: typeof generation === 'number' ? generation : undefined,
  }
}

/** Human sentence for a structured `detail`, or null when nothing is known.
 *
 * The backend answers a blocked read with `{code, reason, truth_status,
 * ledger_generation, …}` and no `message`; stringifying that object used to
 * dump raw JSON on the Барабан / Полки / Очередь screens. Note that `reason`
 * can arrive empty even for the snapshot-missing bodies, because the readiness
 * dict is spread after it — hence the fallback chain down to `code`. */
export function problemMessage(detail: unknown): string | null {
  const problem = problemDetail(detail)
  if (!problem) return null
  const head = problem.message
    ?? problem.reason
    ?? (problem.code ? PROBLEM_CODE_LABEL[problem.code] ?? problem.code : undefined)
  if (!head) return null
  const context: string[] = []
  const statusLabel = problem.truth_status
    ? TRUTH_STATUS_LABEL[problem.truth_status] ?? problem.truth_status
    : undefined
  if (statusLabel) context.push(statusLabel)
  if (typeof problem.ledger_generation === 'number') {
    context.push(`поколение #${problem.ledger_generation}`)
  }
  return context.length ? `${head} (${context.join(', ')})` : head
}

/** Truth-unavailable state of a failed read, or null for a plain error.
 *
 * 503 with a structured body is not a broken screen: the accepted generation or
 * one of its snapshots is simply not there yet, and the page must say so
 * instead of showing a red transport error. */
export function unavailableTruth(error: unknown): { reason: string; code: string | null } | null {
  if (!(error instanceof ApiError) || error.status !== 503) return null
  const problem = problemDetail(error.detail)
  if (!problem) return null
  const reason = problemMessage(error.detail)
  if (!reason) return null
  return { reason, code: problem.code ?? null }
}

async function request(path: string, init?: RequestInit, signal?: AbortSignal): Promise<Response> {
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
        // Structured detail (e.g. 409 {message, deficit_lines}, 503 {code,
        // reason, truth_status, …}) — build a sentence from the known fields
        // and only fall back to the raw object when none of them is present.
        message = problemMessage(detail) ?? JSON.stringify(detail)
      }
    } catch {
      // Keep the original response text when it is not JSON.
    }
    throw new ApiError(message, res.status, detail)
  }

  return res
}

export async function api<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const res = await request(path, init, signal)
  return res.json() as Promise<T>
}

// Transport for endpoints that answer with a document body instead of JSON
// (e.g. the route-sheet printer returns `text/html`). `api()` would call
// `res.json()` on those and always throw, so such endpoints must use this.
export async function apiText(path: string, init?: RequestInit, signal?: AbortSignal): Promise<string> {
  const res = await request(path, init, signal)
  return res.text()
}
