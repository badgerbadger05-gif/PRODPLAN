import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FeasibilityResponse } from '../../domain/releaseFeasibility'
import { analyzeRelease, searchReleaseItems } from '../../services/releaseFeasibility'
import { ReleaseFeasibilityPage } from './ReleaseFeasibilityPage'

vi.mock('../../services/releaseFeasibility', () => ({
  analyzeRelease: vi.fn(),
  searchReleaseItems: vi.fn(),
}))

const mockedAnalyze = vi.mocked(analyzeRelease)
const mockedSearch = vi.mocked(searchReleaseItems)

const response: FeasibilityResponse = {
  root: {
    item_id: 4921,
    item_code: 'НФ-00003999',
    item_article: 'Г0003999',
    item_name: 'Мотобуксировщик',
    unit: 'шт',
    requested_qty: 6,
    stock_on_hand: 0,
    has_spec: true,
  },
  summary: {
    status: 'make',
    shortage_count: 0,
    blocked_count: 0,
    make_count: 1,
    idle_blocker_count: 0,
    items_checked: 2,
    max_level: 1,
    producible_qty: 0,
    fully_producible: false,
    warnings: [],
    cycles: [],
    max_depth: 20,
  },
  blocking: [{
    item_id: 7489,
    item_code: 'НФ-00004858',
    item_article: 'ПФ0004858',
    item_name: 'Рамка тележки',
    unit: 'шт',
    level: 1,
    kind: 'node',
    status: 'make',
    reason: 'Надо изготовить',
    needed_now: true,
    is_blocking: false,
    required_qty: 9,
    stock_on_hand: 0,
    allocated_qty: 0,
    shortage_qty: 9,
    used_in: [],
    warehouses: [
      {
        warehouse_name: 'Участок токарный',
        qty: 66,
        physical_qty: 94,
        reserved_qty: 28,
        counted: true,
      },
      {
        warehouse_name: 'Склад №2',
        qty: 0,
        physical_qty: 3,
        reserved_qty: 69,
        counted: true,
      },
    ],
  }],
  tree: null,
  tree_truncated: false,
}

describe('ReleaseFeasibilityPage warehouse stock', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedSearch.mockResolvedValue({
      items: [{
        item_id: 4921,
        item_code: 'НФ-00003999',
        item_article: 'Г0003999',
        item_name: 'Мотобуксировщик',
        unit: 'шт',
        stock_on_hand: 0,
        has_spec: true,
      }],
      meta: { q: 'Г0003999', count: 1 },
    })
    mockedAnalyze.mockResolvedValue(response)
  })

  it('separates free, physical and custody quantities', async () => {
    render(<ReleaseFeasibilityPage />)

    fireEvent.change(screen.getByLabelText('Артикул изделия'), { target: { value: 'Г0003999' } })
    fireEvent.change(screen.getByLabelText('Количество'), { target: { value: '6' } })
    fireEvent.click(screen.getByRole('button', { name: 'Проверить' }))
    fireEvent.click(await screen.findByText('Рамка тележки'))

    expect(await screen.findByText('66 свободно')).toBeInTheDocument()
    expect(screen.getByText('физически 94 · опека 28')).toBeInTheDocument()
    expect(screen.getByText('0 свободно')).toBeInTheDocument()
    expect(screen.getByText('физически 3 · опека 69')).toBeInTheDocument()
  })
})
