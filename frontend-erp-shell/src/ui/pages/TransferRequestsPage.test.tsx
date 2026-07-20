import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import {
  getMaterialIssue,
  listMaterialIssues,
  markMaterialIssueAssembled,
} from '../../services/productionControl'
import { TransferRequestsPage } from './TransferRequestsPage'

vi.mock('../../services/productionControl', () => ({
  deleteMaterialIssue: vi.fn(),
  getMaterialIssue: vi.fn(),
  listMaterialIssues: vi.fn(),
  markMaterialIssueAssembled: vi.fn(),
}))

const mockedList = vi.mocked(listMaterialIssues)
const mockedDetail = vi.mocked(getMaterialIssue)
const mockedAssembled = vi.mocked(markMaterialIssueAssembled)

const row = {
  issue_id: 5,
  document_number: 'ПМ-000005',
  status: 'exported',
  product_id: 11,
  order_id: 12,
  order_number: 'ЗСНФ-001',
  order_prodplan_number: 'ПП-001',
  order_one_c_number: 'ЗСНФ-001',
  order_ref1c: 'order-ref',
  item_name: 'Корпус редуктора',
  item_article: 'КР-01',
  quantity: 10,
  remaining_qty: 4,
  unit: 'шт',
  source_warehouse_ref1c: 'wh-source',
  source_warehouse_name: 'Заготовительный участок',
  warehouse_ref1c: 'wh-destination',
  destination_warehouse_name: 'Сборочный участок',
  exported_ref1c: 'transfer-ref',
  one_c_number: 'ПТ-000005',
  can_assemble: true,
  line_status: 'to_move',
  lines_count: 1,
}

describe('TransferRequestsPage Doctype migration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedList.mockResolvedValue({
      rows: [row],
      total: 1,
      limit: 100,
      offset: 0,
      source_warehouses: [{
        warehouse_ref1c: 'wh-source',
        warehouse_name: 'Заготовительный участок',
      }],
    })
    mockedDetail.mockResolvedValue({
      ...row,
      lines: [{
        line_id: 9,
        component_item_id: 15,
        item_name: 'Втулка',
        item_article: 'ВТ-15',
        required_qty: 4,
        issued_qty: 3,
        unit: 'шт',
        line_status: 'planned',
      }],
    })
    mockedAssembled.mockResolvedValue({})
  })

  it('preserves two-line rows, dynamic warehouse filter and component detail', async () => {
    render(<MemoryRouter><TransferRequestsPage /></MemoryRouter>)

    expect(await screen.findByText('ПМ-000005')).toBeInTheDocument()
    expect(screen.getByText('Корпус редуктора')).toBeInTheDocument()
    expect(await screen.findByText('Втулка')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Заготовительный участок' })).toBeInTheDocument()
    expect(screen.getByText('нужно 4')).toBeInTheDocument()
    expect(screen.getByText('выдано 3')).toBeInTheDocument()
  })

  it('keeps explicit search submit and the assembled action', async () => {
    render(<MemoryRouter><TransferRequestsPage /></MemoryRouter>)
    await screen.findByText('ПМ-000005')

    fireEvent.change(screen.getByLabelText('Поиск'), { target: { value: 'редуктор' } })
    expect(mockedList).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(screen.getByLabelText('Поиск'), { key: 'Enter' })
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(2))
    expect(mockedList.mock.calls[1]?.[0].get('search')).toBe('редуктор')

    fireEvent.click(screen.getByRole('button', { name: 'Собрано' }))
    await waitFor(() => expect(mockedAssembled).toHaveBeenCalledWith(5))
    expect(await screen.findByText(/обеспечение обновлено: собрано/)).toBeInTheDocument()
  })
})
