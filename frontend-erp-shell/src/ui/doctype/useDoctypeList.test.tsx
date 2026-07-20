import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Doctype } from './types'
import type { AccessSubject } from './permissions'
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

const access: AccessSubject = { roles: ['viewer'], permissions: [] }

describe('useDoctypeList', () => {
  it('loads rows and detail, then resets paging when a filter changes', async () => {
    const doctype = createDoctype()
    const { result } = renderHook(() => useDoctypeList(doctype, { access }))

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
    const { result } = renderHook(() => useDoctypeList(doctype, { access }))
    await waitFor(() => expect(result.current.rows).toHaveLength(1))

    await act(async () => result.current.runAction('run'))

    expect(action).toHaveBeenCalledOnce()
    expect(result.current.message).toBe('Готово')
    await waitFor(() => expect(doctype.dataSource.list).toHaveBeenCalledTimes(2))
  })

  it('passes list metadata to actions and clears selection on request', async () => {
    const doctype = createDoctype()
    doctype.meta.selectionMode = 'multiple'
    doctype.dataSource.list = vi.fn(async () => ({
      rows: [
        { id: 1, title: 'Первая' },
        { id: 2, title: 'Вторая' },
      ],
      total: 2,
      run_id: 17,
    }))
    const action = vi.fn(async () => ({
      message: 'Обработано',
      clearSelection: true,
    }))
    doctype.actions = [{
      key: 'process',
      label: 'Обработать',
      scope: 'selection',
      run: action,
    }]
    const { result } = renderHook(() => useDoctypeList(doctype, { access }))
    await waitFor(() => expect(result.current.listMeta.run_id).toBe(17))

    act(() => result.current.setVisibleSelection(true))
    expect(result.current.selection.map((row) => row.id)).toEqual([1, 2])

    await act(async () => result.current.runAction('process'))

    expect(action).toHaveBeenCalledWith(expect.objectContaining({
      listMeta: expect.objectContaining({ run_id: 17 }),
      selection: [
        expect.objectContaining({ id: 1 }),
        expect.objectContaining({ id: 2 }),
      ],
    }))
    expect(result.current.selectedIds.size).toBe(0)
    expect(result.current.selection).toEqual([])
  })

  it('does not load or run protected resources without access', async () => {
    const doctype = createDoctype()
    doctype.permissions = {
      view: ['planner'],
      actions: { run: 'plan.run' },
    }
    const action = vi.fn(async () => ({ message: 'Нельзя' }))
    doctype.actions = [{ key: 'run', label: 'Запуск', scope: 'global', run: action }]
    const denied: AccessSubject = { roles: ['viewer'], permissions: [] }

    const { result } = renderHook(() => useDoctypeList(doctype, { access: denied }))
    await act(async () => result.current.runAction('run'))

    expect(doctype.dataSource.list).not.toHaveBeenCalled()
    expect(action).not.toHaveBeenCalled()
  })

  it('ignores a late detail response for the previously active row', async () => {
    const doctype = createDoctype()
    let resolveFirst: ((value: Detail) => void) | undefined
    doctype.dataSource.list = vi.fn(async () => ({
      rows: [{ id: 1, title: 'Первая' }, { id: 2, title: 'Вторая' }],
      total: 2,
    }))
    doctype.dataSource.detail = vi.fn((id) => {
      if (id === 1) return new Promise<Detail>((resolve) => { resolveFirst = resolve })
      return Promise.resolve({ id: 2, description: 'Актуальная' })
    })
    const { result } = renderHook(() => useDoctypeList(doctype, { access }))
    await waitFor(() => expect(result.current.activeId).toBe(1))

    act(() => result.current.setActiveId(2))
    await waitFor(() => expect(result.current.detail?.description).toBe('Актуальная'))
    await act(async () => resolveFirst?.({ id: 1, description: 'Устаревшая' }))

    expect(result.current.detail?.description).toBe('Актуальная')
  })

  it('selects and clears only selectable rows from the visible page', async () => {
    const doctype = createDoctype()
    doctype.meta.selectionMode = 'multiple'
    doctype.selectable = (row) => row.id !== 2
    doctype.dataSource.list = vi.fn(async () => ({
      rows: [
        { id: 1, title: 'Доступная' },
        { id: 2, title: 'Уже заказана' },
        { id: 3, title: 'Доступная вторая' },
      ],
      total: 3,
    }))
    const { result } = renderHook(() => useDoctypeList(doctype, { access }))
    await waitFor(() => expect(result.current.rows).toHaveLength(3))

    act(() => result.current.setVisibleSelection(true))
    expect([...result.current.selectedIds]).toEqual([1, 3])
    expect(result.current.selection.map((row) => row.id)).toEqual([1, 3])

    act(() => result.current.setVisibleSelection(false))
    expect([...result.current.selectedIds]).toEqual([])
    expect(result.current.selection).toEqual([])
  })
})
