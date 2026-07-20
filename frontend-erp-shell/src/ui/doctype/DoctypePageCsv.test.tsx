import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { Doctype } from './types'
import { useDoctypeList } from './useDoctypeList'
import { DoctypePage } from './DoctypePage'
import { downloadCsv } from './csvExport'

vi.mock('./csvExport', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./csvExport')>()
  return {
    ...actual,
    downloadCsv: vi.fn(),
  }
})

type Row = { id: number; name: string }
type Filters = Record<string, never>

const rows: Row[] = [
  { id: 1, name: 'Первая' },
  { id: 2, name: 'Вторая' },
]

const doctype: Doctype<Row, Filters> = {
  meta: {
    name: 'csv-current-page',
    title: 'CSV',
    subtitle: '',
    idField: 'id',
    selectionMode: 'multiple',
    exportCsv: {
      filename: 'page.csv',
      rows: 'current-page',
    },
  },
  initialFilters: {},
  dataSource: {
    list: async () => ({ rows, total: rows.length }),
  },
  columns: [
    { key: 'select', title: '', type: 'select-checkbox' },
    { key: 'name', title: 'Название' },
  ],
  permissions: {},
}

function Harness() {
  const state = useDoctypeList(doctype, {
    access: { roles: [], permissions: [] },
  })
  return (
    <DoctypePage
      doctype={doctype}
      state={state}
      access={{ roles: [], permissions: [] }}
    />
  )
}

describe('DoctypePage CSV row scope', () => {
  it('exports every current-page row even when a selection exists', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter><Harness /></MemoryRouter>)

    await screen.findByText('Первая')
    await user.click(screen.getByRole('checkbox', { name: 'Выбрать строку 1' }))
    await user.click(screen.getByRole('button', { name: 'CSV (текущая страница)' }))

    await waitFor(() => {
      expect(downloadCsv).toHaveBeenCalledWith(
        'Название\r\nПервая\r\nВторая\r\n',
        'page.csv',
      )
    })
  })
})
