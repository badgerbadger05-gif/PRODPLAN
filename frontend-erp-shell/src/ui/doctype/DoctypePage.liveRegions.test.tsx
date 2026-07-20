import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { DoctypePage } from './DoctypePage'
import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'

type Row = { id: number; name: string }
type Filters = Record<string, never>

const rows: Row[] = [{ id: 1, name: 'Строка' }]

const doctype: Doctype<Row, Filters> = {
  meta: {
    name: 'live-region-test',
    title: 'Проверка статусов',
    subtitle: '',
    idField: 'id',
  },
  initialFilters: {},
  dataSource: {
    list: async () => ({ rows, total: rows.length }),
  },
  columns: [{ key: 'name', title: 'Название' }],
  permissions: {},
}

function state(
  overrides: Partial<DoctypeListState<Row, Filters, never>> = {},
): DoctypeListState<Row, Filters, never> {
  return {
    rows,
    activeRow: rows[0],
    activeId: 1,
    setActiveId: vi.fn(),
    detail: null,
    detailLoading: false,
    listMeta: {},
    filters: {},
    setFilter: vi.fn(),
    applyFilters: vi.fn(),
    sort: null,
    setSort: vi.fn(),
    applyViewState: vi.fn(),
    selection: [],
    selectedIds: new Set(),
    toggleSelection: vi.fn(),
    setVisibleSelection: vi.fn(),
    paging: {
      limit: 100,
      offset: 0,
      total: 1,
      visibleFrom: 1,
      visibleTo: 1,
      canPrev: false,
      canNext: false,
      prev: vi.fn(),
      next: vi.fn(),
    },
    loading: false,
    listLoading: false,
    actionLoading: false,
    error: '',
    message: '',
    dialog: null,
    closeDialog: vi.fn(),
    actionContext: {
      rows,
      activeRow: rows[0],
      selection: [],
    },
    runAction: vi.fn(),
    reload: vi.fn(),
    ...overrides,
  } as DoctypeListState<Row, Filters, never>
}

function renderPage(overrides: Partial<DoctypeListState<Row, Filters, never>>) {
  return render(
    <MemoryRouter>
      <DoctypePage
        doctype={doctype}
        state={state(overrides)}
        access={{ roles: [], permissions: [] }}
      />
    </MemoryRouter>,
  )
}

describe('DoctypePage live regions', () => {
  it('announces loading progress as a status', () => {
    renderPage({ loading: true, listLoading: true })

    expect(screen.getByRole('status')).toHaveTextContent('Загрузка...')
  })

  it('announces a successful operation message as a status', () => {
    renderPage({ message: 'Операция завершена' })

    expect(screen.getByRole('status')).toHaveTextContent('Операция завершена')
  })

  it('announces an operation error as an alert', () => {
    renderPage({ error: 'Не удалось выполнить операцию' })

    expect(screen.getByRole('alert')).toHaveTextContent('Не удалось выполнить операцию')
  })
})
