import { beforeEach, describe, expect, it } from 'vitest'
import { LocalSavedViewsRepository } from './localStorageRepository'
import type { ViewState } from './types'

const state: ViewState = {
  filters: { search: 'редуктор', urgent: true },
  sort: [{ field: 'required_date', direction: 'asc' }],
  visibleColumns: ['name', 'required_date'],
  density: 'compact',
}

describe('LocalSavedViewsRepository', () => {
  beforeEach(() => localStorage.clear())

  it('creates, updates and removes personal views while preserving timestamps', async () => {
    const times = [new Date('2026-07-20T10:00:00Z'), new Date('2026-07-20T11:00:00Z')]
    const repository = new LocalSavedViewsRepository({
      now: () => times.shift()!,
      createId: () => 'view-1',
    })
    const created = await repository.save({ resource: 'orders', name: '  Срочные  ', state })
    const updated = await repository.save({
      resource: 'orders',
      id: created.id,
      name: 'Срочные закупки',
      state: { ...state, density: 'comfortable' },
    })

    expect(updated).toMatchObject({
      id: 'view-1',
      name: 'Срочные закупки',
      createdAt: '2026-07-20T10:00:00.000Z',
      updatedAt: '2026-07-20T11:00:00.000Z',
    })
    expect((await repository.list('orders'))[0]?.state.density).toBe('comfortable')

    await repository.remove('orders', 'view-1')
    expect(await repository.list('orders')).toEqual([])
  })

  it('stores a default and clears it when its view is removed', async () => {
    const repository = new LocalSavedViewsRepository({ createId: () => 'default' })
    await repository.save({ resource: 'orders', name: 'Мой вид', state, makeDefault: true })
    expect(await repository.getDefaultId('orders')).toBe('default')

    await repository.remove('orders', 'default')
    expect(await repository.getDefaultId('orders')).toBeNull()
  })

  it('isolates resources and returns defensive state copies', async () => {
    const repository = new LocalSavedViewsRepository({ createId: () => 'one' })
    await repository.save({ resource: 'orders', name: 'Заказы', state })
    expect(await repository.list('ledger')).toEqual([])

    const listed = await repository.list('orders')
    ;(listed[0]!.state.visibleColumns as string[]).push('mutation')
    expect((await repository.list('orders'))[0]?.state.visibleColumns).toEqual(['name', 'required_date'])
  })

  it('recovers safely from corrupt and unsupported storage data', async () => {
    localStorage.setItem('prodplan.erp.saved-views', '{broken')
    const repository = new LocalSavedViewsRepository()
    expect(await repository.list('orders')).toEqual([])

    localStorage.setItem('prodplan.erp.saved-views', JSON.stringify({ version: 99, resources: {} }))
    expect(await repository.list('orders')).toEqual([])
  })

  it('rejects an unknown default view', async () => {
    const repository = new LocalSavedViewsRepository()
    await expect(repository.setDefaultId('orders', 'missing')).rejects.toThrow('не найдено')
  })
})
