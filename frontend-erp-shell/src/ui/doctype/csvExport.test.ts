import { describe, expect, it } from 'vitest'
import type { AccessSubject, Doctype } from './types'
import { buildDoctypeCsv } from './csvExport'

type Row = {
  id: number
  name: string
  quantity: number
  confidential: string
}

type Filters = Record<string, never>

const doctype: Doctype<Row, Filters> = {
  meta: {
    name: 'items',
    title: 'Позиции',
    subtitle: '',
    idField: 'id',
    selectionMode: 'multiple',
    exportCsv: true,
  },
  initialFilters: {},
  dataSource: {
    list: async () => ({ rows: [], total: 0 }),
  },
  columns: [
    { key: 'select', title: '', type: 'select-checkbox' },
    { key: 'name', title: 'Название' },
    { key: 'quantity', title: 'Количество', type: 'number' },
    { key: 'confidential', title: 'Себестоимость' },
  ],
  permissions: {
    fields: {
      confidential: 'items.cost.view',
    },
  },
}

const viewer: AccessSubject = {
  roles: ['viewer'],
  permissions: [],
}

describe('generic Doctype CSV export', () => {
  it('exports the requested visible columns and RFC 4180-escapes cell values', () => {
    const csv = buildDoctypeCsv({
      doctype,
      rows: [{
        id: 1,
        name: 'Гайка, "М8"',
        quantity: 12,
        confidential: '100',
      }],
      visibleColumns: ['name', 'quantity', 'confidential'],
      access: viewer,
    })

    expect(csv).toBe('Название,Количество\r\n"Гайка, ""М8""",12\r\n')
    expect(csv).not.toContain('Себестоимость')
    expect(csv).not.toContain('100')
  })

  it('uses a column value accessor and exports every supplied row in order', () => {
    const csvDoctype: Doctype<Row, Filters> = {
      ...doctype,
      columns: [
        {
          key: 'name',
          title: 'Позиция',
          value: (row) => `${row.id}: ${row.name}`,
        },
      ],
      permissions: {},
    }

    expect(buildDoctypeCsv({
      doctype: csvDoctype,
      rows: [
        { id: 2, name: 'Шайба', quantity: 5, confidential: '' },
        { id: 3, name: 'Болт', quantity: 8, confidential: '' },
      ],
      visibleColumns: ['name'],
      access: viewer,
    })).toBe('Позиция\r\n2: Шайба\r\n3: Болт\r\n')
  })
})
