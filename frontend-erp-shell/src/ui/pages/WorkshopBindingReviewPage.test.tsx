import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BindingReviewItem, BindingReviewLine } from '../../domain/workshopBindingReview'
import { addResourceProductionKind, listResources } from '../../services/resources'
import {
  assignLineWorkshop,
  listReviewItemLines,
  listReviewItems,
} from '../../services/workshopBindingReview'
import { WorkshopBindingReviewPage } from './WorkshopBindingReviewPage'

vi.mock('../../services/resources', () => ({
  addResourceProductionKind: vi.fn(),
  listResources: vi.fn(),
}))

vi.mock('../../services/workshopBindingReview', () => ({
  assignLineWorkshop: vi.fn(),
  listReviewItemLines: vi.fn(),
  listReviewItems: vi.fn(),
}))

const activeItem: BindingReviewItem = {
  item_id: 41,
  item_code: 'BEARING-01',
  item_name: 'Подшипник ведущего вала',
  item_article: 'ПД-01',
  active_lines: 1,
  reason_code: 'KIND_NOT_BOUND',
  reason_text: 'Вид производства не привязан к участку.',
  recommendation: 'Проверьте предложенный участок и подтвердите привязку.',
  spec_id: 3,
  spec_name: 'СП-03',
  production_kind_id: 5,
  production_kind_name: 'Мехобработка',
  suggested_resource_id: 1,
  suggested_resource_name: 'Механический участок',
  suggested_stage_id: 8,
  suggested_stage_name: 'Токарная обработка',
}

const catalogItem: BindingReviewItem = {
  ...activeItem,
  item_id: 42,
  item_code: 'CATALOG-02',
  item_article: 'КТ-02',
  item_name: 'Деталь из справочника',
  active_lines: 0,
}

const orderLine: BindingReviewLine = {
  product_id: 900,
  order_id: 700,
  order_number: 'ЗСНФ-000700',
  quantity: 10,
  remaining_qty: 4,
  status: 'ready',
}

function response(item: BindingReviewItem, scope: 'active' | 'catalog' = 'active') {
  return {
    items: [item],
    total: 1,
    limit: 100,
    offset: 0,
    scope,
    counts_by_reason: {
      NO_SPEC: 2,
      NO_PRODUCTION_KIND: 3,
      KIND_NOT_BOUND: 1,
      NO_WAREHOUSE_BINDING: 4,
    },
  }
}

describe('WorkshopBindingReviewPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listReviewItems).mockImplementation(async ({ scope }) => (
      scope === 'catalog' ? response(catalogItem, 'catalog') : response(activeItem)
    ))
    vi.mocked(listReviewItemLines).mockResolvedValue({
      item_id: activeItem.item_id,
      rows: [orderLine],
      total: 1,
    })
    vi.mocked(listResources).mockResolvedValue([
      { resource_id: 1, resource_name: 'Механический участок' },
      { resource_id: 2, resource_name: 'Сборочный участок' },
    ])
    vi.mocked(addResourceProductionKind).mockResolvedValue({
      id: 10,
      resource_id: 1,
      production_kind_id: 5,
      production_kind_name: 'Мехобработка',
    })
    vi.mocked(assignLineWorkshop).mockResolvedValue({})
  })

  it('loads the active list, reason counts and selected item detail', async () => {
    render(<WorkshopBindingReviewPage />)

    expect((await screen.findAllByText('Подшипник ведущего вала')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Вид не привязан к участку (1)' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Нет спецификации (2)' })).toBeVisible()
    expect(screen.getByText('Вид производства не привязан к участку.')).toBeVisible()
    expect(await screen.findByText(/ЗСНФ-000700 · 4 шт/)).toBeVisible()

    expect(listReviewItems).toHaveBeenCalledWith(expect.objectContaining({
      scope: 'active',
      limit: 100,
      offset: 0,
    }))
    expect(listReviewItemLines).toHaveBeenCalledWith(41)
  })

  it('submits search explicitly and applies reason and scope controls', async () => {
    const user = userEvent.setup()
    render(<WorkshopBindingReviewPage />)
    await screen.findAllByText('Подшипник ведущего вала')

    const search = screen.getByPlaceholderText('Поиск: наименование / артикул')
    await user.type(search, 'подшипник')
    expect(listReviewItems).toHaveBeenCalledTimes(1)
    await user.keyboard('{Enter}')
    await waitFor(() => expect(listReviewItems).toHaveBeenCalledWith(expect.objectContaining({
      scope: 'active',
      search: 'подшипник',
    })))

    await user.click(screen.getByRole('button', { name: 'Нет спецификации (2)' }))
    await waitFor(() => expect(listReviewItems).toHaveBeenCalledWith(expect.objectContaining({
      scope: 'active',
      reasonCode: 'NO_SPEC',
    })))

    await user.click(screen.getByRole('button', { name: 'Весь справочник' }))
    await waitFor(() => expect(listReviewItems).toHaveBeenCalledWith(expect.objectContaining({
      scope: 'catalog',
      reasonCode: undefined,
    })))
    expect((await screen.findAllByText('Деталь из справочника')).length).toBeGreaterThan(0)
  })

  it('does not load order lines for a catalog item without active lines', async () => {
    const user = userEvent.setup()
    render(<WorkshopBindingReviewPage />)
    await screen.findByText(/ЗСНФ-000700 · 4 шт/)

    await user.click(screen.getByRole('button', { name: 'Весь справочник' }))
    expect((await screen.findAllByText('Деталь из справочника')).length).toBeGreaterThan(0)
    await waitFor(() => expect(listReviewItems).toHaveBeenCalledWith(expect.objectContaining({
      scope: 'catalog',
    })))

    expect(listReviewItemLines).not.toHaveBeenCalledWith(catalogItem.item_id)
  })

  it('binds the production kind and assigns an order line workshop', async () => {
    const user = userEvent.setup()
    render(<WorkshopBindingReviewPage />)
    await screen.findByText(/ЗСНФ-000700 · 4 шт/)

    await user.click(screen.getByRole('button', { name: 'Привязать вид → участок' }))
    await waitFor(() => expect(addResourceProductionKind).toHaveBeenCalledWith(1, 5))
    expect(await screen.findByText('Вид «Мехобработка» привязан к участку. Список обновлён.')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Назначить' }))
    await waitFor(() => expect(assignLineWorkshop).toHaveBeenCalledWith(900, 1))
    expect(await screen.findByText('Строке заказа ЗСНФ-000700 назначен участок вручную.')).toBeVisible()
    expect(listReviewItems).toHaveBeenCalledTimes(3)
  })
})
