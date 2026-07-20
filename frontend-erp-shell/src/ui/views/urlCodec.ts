import type {
  ViewDensity,
  ViewFilterValue,
  ViewSort,
  ViewState,
} from './types'

export const VIEW_STATE_URL_VERSION = 1

interface ViewStateUrlEnvelope {
  v: typeof VIEW_STATE_URL_VERSION
  state: ViewState
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasOnlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const allowed = new Set(keys)
  return Object.keys(value).every((key) => allowed.has(key))
}

function isFilterValue(value: unknown): value is ViewFilterValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (!Array.isArray(value)) return false
  return value.every((item) => typeof item === 'string')
    || value.every((item) => typeof item === 'number' && Number.isFinite(item))
}

function isDensity(value: unknown): value is ViewDensity {
  return value === 'compact' || value === 'comfortable'
}

function isSort(value: unknown): value is ViewSort {
  if (!isRecord(value) || !hasOnlyKeys(value, ['field', 'direction'])) return false
  return typeof value.field === 'string'
    && (value.direction === 'asc' || value.direction === 'desc')
}

export function isViewState(value: unknown): value is ViewState {
  if (!isRecord(value) || !hasOnlyKeys(value, ['filters', 'sort', 'visibleColumns', 'density'])) {
    return false
  }
  return isRecord(value.filters)
    && Object.values(value.filters).every(isFilterValue)
    && Array.isArray(value.sort)
    && value.sort.every(isSort)
    && Array.isArray(value.visibleColumns)
    && value.visibleColumns.every((column) => typeof column === 'string')
    && isDensity(value.density)
}

function encodeBase64Url(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/u, '')
}

function decodeBase64Url(value: string): string {
  if (!/^[A-Za-z0-9_-]+$/u.test(value)) throw new Error('Invalid base64url')
  const padding = '='.repeat((4 - value.length % 4) % 4)
  const binary = atob(value.replaceAll('-', '+').replaceAll('_', '/') + padding)
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
}

/**
 * Encodes a complete table view into an opaque, versioned URL-safe value.
 * The result can be placed directly in a URLSearchParams value.
 */
export function encodeViewState(state: ViewState): string {
  if (!isViewState(state)) throw new TypeError('Invalid view state')
  const envelope: ViewStateUrlEnvelope = { v: VIEW_STATE_URL_VERSION, state }
  return encodeBase64Url(JSON.stringify(envelope))
}

/**
 * Returns null for corrupt, unsupported or structurally invalid URL values.
 * Consumers can then retain their current/default view without an exception.
 */
export function decodeViewState(value: string | null | undefined): ViewState | null {
  if (!value) return null
  try {
    const parsed: unknown = JSON.parse(decodeBase64Url(value))
    if (!isRecord(parsed) || !hasOnlyKeys(parsed, ['v', 'state'])) return null
    if (parsed.v !== VIEW_STATE_URL_VERSION || !isViewState(parsed.state)) return null
    return parsed.state
  } catch {
    return null
  }
}
