import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { LocalSavedViewsRepository } from './localStorageRepository'
import type { ViewState } from './types'
import { useSavedViews } from './useSavedViews'

const initial: ViewState = {
  filters: {},
  sort: [],
  visibleColumns: ['name'],
  density: 'comfortable',
}
const compact: ViewState = {
  filters: { status: 'open' },
  sort: [{ field: 'date', direction: 'desc' }],
  visibleColumns: ['name', 'status'],
  density: 'compact',
}

describe('useSavedViews', () => {
  beforeEach(() => localStorage.clear())

  it('loads and applies the personal default view', async () => {
    const repository = new LocalSavedViewsRepository({ createId: () => 'open' })
    await repository.save({ resource: 'orders', name: 'Открытые', state: compact, makeDefault: true })

    const { result } = renderHook(() => useSavedViews('orders', initial, repository))
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.activeViewId).toBe('open')
    expect(result.current.state).toEqual(compact)
  })

  it('saves current state, changes default and resets after removal', async () => {
    const repository = new LocalSavedViewsRepository({ createId: () => 'mine' })
    const { result } = renderHook(() => useSavedViews('orders', initial, repository))
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.setState(compact))
    await act(async () => { await result.current.save('Мой вид', { makeDefault: true }) })
    expect(result.current.defaultViewId).toBe('mine')
    expect(result.current.views).toHaveLength(1)

    await act(async () => { await result.current.remove('mine') })
    expect(result.current.activeViewId).toBeNull()
    expect(result.current.defaultViewId).toBeNull()
    expect(result.current.state).toEqual(initial)
  })

  it('can switch between a saved view and the unsaved initial state', async () => {
    const repository = new LocalSavedViewsRepository({ createId: () => 'open' })
    await repository.save({ resource: 'orders', name: 'Открытые', state: compact })
    const { result } = renderHook(() => useSavedViews('orders', initial, repository))
    await waitFor(() => expect(result.current.views).toHaveLength(1))

    act(() => result.current.apply('open'))
    expect(result.current.state.density).toBe('compact')
    act(() => result.current.apply(null))
    expect(result.current.state).toEqual(initial)
  })
})
