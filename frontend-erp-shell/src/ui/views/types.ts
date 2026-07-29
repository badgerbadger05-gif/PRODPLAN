export type ViewDensity = 'compact' | 'comfortable'

export type ViewFilterValue =
  | string
  | number
  | boolean
  | null
  | readonly string[]
  | readonly number[]

export type ViewFilters = Readonly<Record<string, ViewFilterValue>>

export interface ViewSort {
  field: string
  direction: 'asc' | 'desc'
}

export interface ViewState {
  filters: ViewFilters
  sort: readonly ViewSort[]
  visibleColumns: readonly string[]
  density: ViewDensity
}

export interface SavedView {
  id: string
  resource: string
  name: string
  state: ViewState
  createdAt: string
  updatedAt: string
}

export interface SaveViewInput {
  id?: string
  resource: string
  name: string
  state: ViewState
  makeDefault?: boolean
}

/**
 * Persistence boundary for personal views. A remote implementation can replace
 * localStorage without changing consumers.
 */
export interface SavedViewsRepository {
  list(resource: string): Promise<readonly SavedView[]>
  save(input: SaveViewInput): Promise<SavedView>
  remove(resource: string, id: string): Promise<void>
  getDefaultId(resource: string): Promise<string | null>
  setDefaultId(resource: string, id: string | null): Promise<void>
}

export function cloneViewState(state: ViewState): ViewState {
  return {
    filters: { ...state.filters },
    sort: state.sort.map((sort) => ({ ...sort })),
    visibleColumns: [...state.visibleColumns],
    density: state.density,
  }
}
