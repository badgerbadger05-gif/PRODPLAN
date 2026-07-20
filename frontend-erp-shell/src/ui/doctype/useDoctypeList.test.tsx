import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Doctype } from './types'
import { useDoctypeList } from './useDoctypeList'

type Row = { id: number; title: string }
type Filters = { search: string }
type Detail = { id: number; description: string }

function createDoctype(): Doctype<Row, Filters, Detail> {
  return {
    meta: {
      name: 'test',
      title: 'Тест',
      subtitle: 'Тестовый журнал',
      idField: 'id',
    },
    initialFilters: { search: '' },
    dataSource: {
      list: vi.fn(async ({ limit, offset, filters }) => ({
        rows: [{ id: offset + 1, title: filters.search || 'Первая' }],
        total: 201,
        limit,
        offset,
      })),
      detail: vi.fn(async (id) => ({ id: Number(id), description: 'Деталь' })),
    },
    columns: [{ key: 'title', title: 'Название', type: 'text' }],
    permissions: { view: ['viewer'] },
  }
}

describe('useDoctypeList', () => {
  it('loads rows and detail, then resets paging when a filter changes', async () => {
    const doctype = createDoctype()
    const { result } = renderHook(() => useDoctypeList(doctype))

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    await waitFor(() => expect(result.current.detail?.id).toBe(1))

    act(() => result.current.paging.next())
    await waitFor(() => expect(result.current.paging.offset).toBe(100))

    act(() => result.current.setFilter('search', 'Редуктор'))
    await waitFor(() => expect(result.current.paging.offset).toBe(0))
    await waitFor(() => expect(result.current.rows[0]?.title).toBe('Редуктор'))
  })

  it('runs a declarative action and reloads the list', async () => {
    const doctype = createDoctype()
    const action = vi.fn(async () => ({ message: 'Готово', reload: true }))
    doctype.actions = [{
      key: 'run',
      label: 'Запустить',
      scope: 'global',
      run: action,
    }]
    const { result } = renderHook(() => useDoctypeList(doctype))
    await waitFor(() => expect(result.current.rows).toHaveLength(1))

    await act(async () => result.current.runAction('run'))

    expect(action).toHaveBeenCalledOnce()
    expect(result.current.message).toBe('Готово')
    await waitFor(() => expect(doctype.dataSource.list).toHaveBeenCalledTimes(2))
  })
})

