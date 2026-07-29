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
    filters: { search: '' },
    listMeta: {},
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
    render(<DoctypeTable doctype={doctype} state={state()} access={{ roles: [], permissions: [] }} />)

    expect(screen.getByRole('columnheader', { name: /Наименование/ })).toHaveAttribute('aria-sort', 'descending')
    expect(screen.getByRole('row', { name: /Первая/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('row', { name: /Вторая/ })).toHaveAttribute('aria-selected', 'false')
  })

  it('moves the active row with arrows and opens it with Enter', () => {
    const setActiveId = vi.fn()
    const open = vi.fn()
    render(<DoctypeTable doctype={doctype} state={state({ setActiveId })} access={{ roles: [], permissions: [] }} onRowDoubleClick={open} />)

    const first = screen.getByRole('row', { name: /Первая/ })
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(setActiveId).toHaveBeenCalledWith(2)
    expect(screen.getByRole('row', { name: /Вторая/ })).toHaveFocus()

    fireEvent.keyDown(first, { key: 'Enter' })
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
  })
})

describe('DoctypeTable contextual presentation', () => {
  const contextualDoctype: Doctype<Row, Filters> = {
    ...doctype,
    meta: {
      ...doctype.meta,
      emptyLabel: ({ filters, listMeta }: {
        filters: Filters
        listMeta: Record<string, unknown>
      }) => filters.search
        ? `По запросу «${filters.search}» ничего не найдено`
        : String(listMeta.emptyLabel ?? 'Нет позиций'),
    },
    columns: [
      { key: 'id', title: 'ID' },
      {
        key: 'name',
        title: 'Наименование',
        visible: ({ filters, listMeta }: {
          filters: Filters
          listMeta: Record<string, unknown>
        }) => filters.search === 'подробно' && listMeta.mode === 'detailed',
      },
    ],
  }

  it('shows a conditional column only when filters and list metadata allow it', () => {
    const { rerender } = render(
      <DoctypeTable
        doctype={contextualDoctype}
        state={state({
          filters: { search: '' },
          listMeta: { mode: 'compact' },
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    expect(screen.getByRole('columnheader', { name: 'ID' })).toBeVisible()
    expect(screen.queryByRole('columnheader', { name: 'Наименование' })).not.toBeInTheDocument()
    expect(screen.queryByText('Первая')).not.toBeInTheDocument()

    rerender(
      <DoctypeTable
        doctype={contextualDoctype}
        state={state({
          filters: { search: 'подробно' },
          listMeta: { mode: 'detailed' },
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    expect(screen.getByRole('columnheader', { name: 'Наименование' })).toBeVisible()
    expect(screen.getByText('Первая')).toBeVisible()
  })

  it('renders a context-aware empty label across the currently visible columns', () => {
    render(
      <DoctypeTable
        doctype={contextualDoctype}
        state={state({
          rows: [],
          activeRow: null,
          activeId: null,
          filters: { search: 'редуктор' },
          listMeta: { mode: 'compact' },
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    const empty = screen.getByText('По запросу «редуктор» ничего не найдено')
    expect(empty).toBeVisible()
    expect(empty.closest('td')).toHaveAttribute('colspan', '1')
  })

  it('uses the standard empty label when a doctype does not override it', () => {
    render(
      <DoctypeTable
        doctype={doctype}
        state={state({ rows: [], activeRow: null, activeId: null })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    expect(screen.getByText('Нет данных')).toBeVisible()
  })
})

describe('DoctypeTable bulk selection', () => {
  const multipleDoctype: Doctype<Row, Filters> = {
    ...doctype,
    meta: { ...doctype.meta, selectionMode: 'multiple' },
    columns: [
      { key: 'select', title: '', type: 'select-checkbox' },
      ...doctype.columns,
    ],
  }

  it('selects and clears every row visible on the current page from the header', () => {
    const setVisibleSelection = vi.fn()
    const { rerender } = render(
      <DoctypeTable
        doctype={multipleDoctype}
        state={state({ setVisibleSelection })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    const selectAll = screen.getByRole('checkbox', { name: 'Выбрать все видимые строки' })
    expect(selectAll).not.toBeChecked()

    fireEvent.click(selectAll)
    expect(setVisibleSelection).toHaveBeenCalledWith(true)

    rerender(
      <DoctypeTable
        doctype={multipleDoctype}
        state={state({
          selectedIds: new Set([1, 2]),
          setVisibleSelection,
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    expect(screen.getByRole('checkbox', { name: 'Выбрать все видимые строки' })).toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Выбрать все видимые строки' }))
    expect(setVisibleSelection).toHaveBeenLastCalledWith(false)
  })

  it('marks the header checkbox as indeterminate for a partial visible selection', () => {
    render(
      <DoctypeTable
        doctype={multipleDoctype}
        state={state({
          selectedIds: new Set([1]),
          setVisibleSelection: vi.fn(),
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    expect(screen.getByRole('checkbox', { name: 'Выбрать все видимые строки' })).toHaveProperty('indeterminate', true)
  })

  it('does not render the bulk checkbox for single-selection doctypes', () => {
    render(<DoctypeTable doctype={doctype} state={state()} access={{ roles: [], permissions: [] }} />)

    expect(screen.queryByRole('checkbox', { name: 'Выбрать все видимые строки' })).not.toBeInTheDocument()
  })

  it('counts only selectable rows and explains why another row is disabled', () => {
    const constrainedDoctype: Doctype<Row, Filters> = {
      ...multipleDoctype,
      selectable: (row) => row.id === 1,
      selectionDisabledReason: (row) => `Строка ${row.id} уже обработана`,
    }
    const toggleSelection = vi.fn()
    render(
      <DoctypeTable
        doctype={constrainedDoctype}
        state={state({
          selectedIds: new Set([1]),
          toggleSelection,
          setVisibleSelection: vi.fn(),
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    expect(screen.getByRole('checkbox', { name: 'Выбрать все видимые строки' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Выбрать строку 1' })).toBeEnabled()

    const unavailable = screen.getByRole('checkbox', { name: 'Выбрать строку 2' })
    expect(unavailable).toBeDisabled()
    expect(unavailable).toHaveAttribute('title', 'Строка 2 уже обработана')
    fireEvent.click(unavailable)
    expect(toggleSelection).not.toHaveBeenCalledWith(2)
  })

  it('does not mark select-all as indeterminate for selected non-selectable rows', () => {
    const constrainedDoctype: Doctype<Row, Filters> = {
      ...multipleDoctype,
      selectable: (row) => row.id === 1,
    }
    render(
      <DoctypeTable
        doctype={constrainedDoctype}
        state={state({
          selectedIds: new Set([2]),
          setVisibleSelection: vi.fn(),
        })}
        access={{ roles: [], permissions: [] }}
      />,
    )

    const selectAll = screen.getByRole('checkbox', { name: 'Выбрать все видимые строки' })
    expect(selectAll).not.toBeChecked()
    expect(selectAll).toHaveProperty('indeterminate', false)
  })
})
