import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DoctypeTable } from './DoctypeTable'
import { FormRenderer } from './FormRenderer'
import type { AccessSubject, Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'

type Row = {
  id: number
  name: string
  confidential: string
  restricted: boolean
}

type Filters = Record<string, never>

const access: AccessSubject = {
  roles: ['viewer'],
  permissions: [],
}

const doctype: Doctype<Row, Filters> = {
  meta: {
    name: 'granular-rbac-test',
    title: 'Проверка детальных прав',
    subtitle: '',
    idField: 'id',
    selectionMode: 'multiple',
  },
  initialFilters: {},
  dataSource: {
    list: async () => ({ rows: [], total: 0 }),
  },
  columns: [
    { key: 'select', title: '', type: 'select-checkbox' },
    { key: 'name', title: 'Наименование' },
    { key: 'confidential', title: 'Закрытая колонка' },
  ],
  detail: {
    sections: [{
      fields: [
        { key: 'name', label: 'Наименование' },
        { key: 'confidential', label: 'Закрытое поле' },
      ],
    }],
  },
  permissions: {
    recordView: (row) => !row.restricted,
    fields: {
      confidential: 'records.confidential.view',
    },
  },
}

function createState() {
  const rows: Row[] = [
    { id: 1, name: 'Доступная строка', confidential: 'Скрытое значение 1', restricted: false },
    { id: 2, name: 'Закрытая строка', confidential: 'Скрытое значение 2', restricted: true },
  ]
  return {
    rows,
    activeRow: rows[0],
    activeId: 1,
    setActiveId: vi.fn(),
    selectedIds: new Set<number>([2]),
    toggleSelection: vi.fn(),
    selection: [rows[1]],
    sort: null,
    setSort: vi.fn(),
  } as unknown as DoctypeListState<Row, Filters, never>
}

describe('Doctype granular RBAC', () => {
  it('does not render or allow selection of a hidden record', () => {
    const state = createState()
    render(<DoctypeTable doctype={doctype} state={state} access={access} />)

    expect(screen.getByText('Доступная строка')).toBeVisible()
    expect(screen.queryByText('Закрытая строка')).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Выбрать строку 2' })).not.toBeInTheDocument()

    fireEvent.keyDown(screen.getByRole('row', { name: /Доступная строка/ }), { key: 'End' })
    expect(state.setActiveId).toHaveBeenLastCalledWith(1)
    expect(state.toggleSelection).not.toHaveBeenCalledWith(2)
  })

  it('does not expose a forbidden column header or its cell values', () => {
    render(<DoctypeTable doctype={doctype} state={createState()} access={access} />)

    expect(screen.queryByRole('columnheader', { name: 'Закрытая колонка' })).not.toBeInTheDocument()
    expect(screen.queryByText('Скрытое значение 1')).not.toBeInTheDocument()
    expect(screen.queryByText('Скрытое значение 2')).not.toBeInTheDocument()
  })

  it('does not expose a forbidden field in the detail form', () => {
    const value: Row = {
      id: 1,
      name: 'Доступная строка',
      confidential: 'Секрет карточки',
      restricted: false,
    }
    render(
      <FormRenderer
        value={value}
        layout={doctype.detail!}
        access={access}
        permissions={doctype.permissions}
      />,
    )

    expect(screen.getByText('Наименование')).toBeVisible()
    expect(screen.getByText('Доступная строка')).toBeVisible()
    expect(screen.queryByText('Закрытое поле')).not.toBeInTheDocument()
    expect(screen.queryByText('Секрет карточки')).not.toBeInTheDocument()
  })
})
