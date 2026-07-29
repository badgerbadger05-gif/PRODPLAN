import {
  cloneViewState,
  type SaveViewInput,
  type SavedView,
  type SavedViewsRepository,
  type ViewDensity,
  type ViewFilterValue,
  type ViewState,
} from './types'

const STORAGE_VERSION = 1

interface StorageEnvelope {
  version: typeof STORAGE_VERSION
  resources: Record<string, {
    defaultId: string | null
    views: SavedView[]
  }>
}

export interface LocalSavedViewsRepositoryOptions {
  storage?: Storage
  storageKey?: string
  now?: () => Date
  createId?: () => string
}

const emptyEnvelope = (): StorageEnvelope => ({ version: STORAGE_VERSION, resources: {} })

function isFilterValue(value: unknown): value is ViewFilterValue {
  return value === null
    || ['string', 'number', 'boolean'].includes(typeof value)
    || (Array.isArray(value) && value.every((item) => typeof item === 'string' || typeof item === 'number'))
}

function isDensity(value: unknown): value is ViewDensity {
  return value === 'compact' || value === 'comfortable'
}

function isViewState(value: unknown): value is ViewState {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<ViewState>
  return !!candidate.filters
    && typeof candidate.filters === 'object'
    && Object.values(candidate.filters).every(isFilterValue)
    && Array.isArray(candidate.sort)
    && candidate.sort.every((sort) => !!sort
      && typeof sort.field === 'string'
      && (sort.direction === 'asc' || sort.direction === 'desc'))
    && Array.isArray(candidate.visibleColumns)
    && candidate.visibleColumns.every((column) => typeof column === 'string')
    && isDensity(candidate.density)
}

function isSavedView(value: unknown, resource: string): value is SavedView {
  if (!value || typeof value !== 'object') return false
  const view = value as Partial<SavedView>
  return typeof view.id === 'string'
    && view.resource === resource
    && typeof view.name === 'string'
    && typeof view.createdAt === 'string'
    && typeof view.updatedAt === 'string'
    && isViewState(view.state)
}

function parseEnvelope(raw: string | null): StorageEnvelope {
  if (!raw) return emptyEnvelope()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return emptyEnvelope()
    const candidate = parsed as Partial<StorageEnvelope>
    if (candidate.version !== STORAGE_VERSION || !candidate.resources || typeof candidate.resources !== 'object') {
      return emptyEnvelope()
    }
    const resources: StorageEnvelope['resources'] = {}
    for (const [resource, collection] of Object.entries(candidate.resources)) {
      if (!collection || typeof collection !== 'object') continue
      const value = collection as { defaultId?: unknown; views?: unknown }
      const views = Array.isArray(value.views)
        ? value.views.filter((view) => isSavedView(view, resource))
        : []
      const defaultId = typeof value.defaultId === 'string' && views.some((view) => view.id === value.defaultId)
        ? value.defaultId
        : null
      resources[resource] = { defaultId, views }
    }
    return { version: STORAGE_VERSION, resources }
  } catch {
    return emptyEnvelope()
  }
}

function defaultIdFactory(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `view-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export class LocalSavedViewsRepository implements SavedViewsRepository {
  private readonly storage: Storage
  private readonly storageKey: string
  private readonly now: () => Date
  private readonly createId: () => string

  constructor(options: LocalSavedViewsRepositoryOptions = {}) {
    this.storage = options.storage ?? window.localStorage
    this.storageKey = options.storageKey ?? 'prodplan.erp.saved-views'
    this.now = options.now ?? (() => new Date())
    this.createId = options.createId ?? defaultIdFactory
  }

  async list(resource: string): Promise<readonly SavedView[]> {
    return this.read().resources[resource]?.views.map((view) => this.clone(view)) ?? []
  }

  async save(input: SaveViewInput): Promise<SavedView> {
    const name = input.name.trim()
    if (!name) throw new Error('Название представления не может быть пустым')

    const envelope = this.read()
    const collection = envelope.resources[input.resource] ?? { defaultId: null, views: [] }
    const existing = input.id ? collection.views.find((view) => view.id === input.id) : undefined
    const timestamp = this.now().toISOString()
    const view: SavedView = {
      id: existing?.id ?? this.createId(),
      resource: input.resource,
      name,
      state: cloneViewState(input.state),
      createdAt: existing?.createdAt ?? timestamp,
      updatedAt: timestamp,
    }
    collection.views = existing
      ? collection.views.map((item) => item.id === view.id ? view : item)
      : [...collection.views, view]
    if (input.makeDefault) collection.defaultId = view.id
    envelope.resources[input.resource] = collection
    this.write(envelope)
    return this.clone(view)
  }

  async remove(resource: string, id: string): Promise<void> {
    const envelope = this.read()
    const collection = envelope.resources[resource]
    if (!collection) return
    collection.views = collection.views.filter((view) => view.id !== id)
    if (collection.defaultId === id) collection.defaultId = null
    this.write(envelope)
  }

  async getDefaultId(resource: string): Promise<string | null> {
    return this.read().resources[resource]?.defaultId ?? null
  }

  async setDefaultId(resource: string, id: string | null): Promise<void> {
    const envelope = this.read()
    const collection = envelope.resources[resource] ?? { defaultId: null, views: [] }
    if (id !== null && !collection.views.some((view) => view.id === id)) {
      throw new Error('Нельзя назначить представление по умолчанию: оно не найдено')
    }
    collection.defaultId = id
    envelope.resources[resource] = collection
    this.write(envelope)
  }

  private read(): StorageEnvelope {
    return parseEnvelope(this.storage.getItem(this.storageKey))
  }

  private write(envelope: StorageEnvelope): void {
    this.storage.setItem(this.storageKey, JSON.stringify(envelope))
  }

  private clone(view: SavedView): SavedView {
    return { ...view, state: cloneViewState(view.state) }
  }
}
