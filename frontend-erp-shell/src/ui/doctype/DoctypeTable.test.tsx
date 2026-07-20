import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Doctype } from './types'
import { DoctypeTable } from './DoctypeTable'
import type { DoctypeListState } from './useDoctypeList'

type Row = { id: number; name: string }
type Filters = { search: string }

const doctype: Doctype<Row, Filters> = {
  meta: {
    name: 'item',
    title: 'Позиции',
    subtitle: '',
    idField: 'id',
    selectionMode: 'single',
  },
  initialFilters: { search: '' },
  dataSource: { list: async () => ({ rows: [], total: 0 }) },
  columns: [
    { key: 'name', title: 'Наименование', sortable: true },
  ],
  permissions: {},
}

function state(overrides: Partial<DoctypeListState<Row, Filters, never>> = {}) {
  const rows = [{ id: 1, name: 'Первая' }, { id: 2, name: 'Вторая' }]
  return {
    rows,
    activeRow: rows[0],
    activeId: 1,
    setActiveId: vi.fn(),
    sort: { sortBy: 'name', sortDir: 'desc' as const },
    setSort: vi.fn(),
    selectedIds: new Set<number>(),
    toggleSelection: vi.fn(),
    ...overrides,
  } as unknown as DoctypeListState<Row, Filters, never>
}

describe('DoctypeTable accessibility', () => {
  it('announces sorting and active row state', () => {
    render(<DoctypeTable doctype={doctype} state={state()} />)

    expect(screen.getByRole('columnheader', { name: /Наименование/ })).toHaveAttribute('aria-sort', 'descending')
    expect(screen.getByRole('row', { name: /Первая/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('row', { name: /Вторая/ })).toHaveAttribute('aria-selected', 'false')
  })

  it('moves the active row with arrows and opens it with Enter', () => {
    const setActiveId = vi.fn()
    const open = vi.fn()
    render(<DoctypeTable doctype={doctype} state={state({ setActiveId })} onRowDoubleClick={open} />)

    const first = screen.getByRole('row', { name: /Первая/ })
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(setActiveId).toHaveBeenCalledWith(2)
    expect(screen.getByRole('row', { name: /Вторая/ })).toHaveFocus()

    fireEvent.keyDown(first, { key: 'Enter' })
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
  })
})
