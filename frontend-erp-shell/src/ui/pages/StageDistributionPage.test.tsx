import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ResourceDistributionResponse } from '../../domain/stageDistribution'
import { calculateResourceDistribution } from '../../services/stageDistribution'
import { StageDistributionPage } from './StageDistributionPage'

vi.mock('../../services/stageDistribution', () => ({
  calculateResourceDistribution: vi.fn(),
}))

const distribution: ResourceDistributionResponse = {
  asOf: '2026-07-20T12:00:00',
  resources: [
    {
      resource_id: 1,
      resource_name: 'Механический участок',
      norm_hours: 7,
      products: [
        {
          root_item_id: 100,
          root_item_code: 'PUMP-01',
          root_item_name: 'Насос',
          components: [{
            item_id: 501,
            item_code: 'SHAFT-01',
            item_article: 'ВАЛ-01',
            item_name: 'Вал ведущий',
            qty_per_unit: 2,
            stock_qty: 6,
            norm_hours: 0.5,
            norm_hours_total: 2,
            stage_id: 8,
            stage_name: 'Токарная обработка',
          }],
        },
        {
          root_item_id: 101,
          root_item_code: 'GEAR-01',
          root_item_name: 'Редуктор',
          components: [{
            item_id: 501,
            item_code: 'SHAFT-01',
            item_article: 'ВАЛ-01',
            item_name: 'Вал ведущий',
            qty_per_unit: 3,
            stock_qty: 6,
            norm_hours: 0.5,
            norm_hours_total: 3,
            stage_id: 8,
            stage_name: 'Токарная обработка',
          }],
        },
      ],
    },
    {
      resource_id: 2,
      resource_name: 'Сборочный участок',
      norm_hours: 1.5,
      products: [{
        root_item_id: 100,
        root_item_code: 'PUMP-01',
        root_item_name: 'Насос',
        components: [{
          item_id: 601,
          item_code: 'BODY-01',
          item_article: 'КОРП-01',
          item_name: 'Корпус насоса',
          qty_per_unit: 1,
          stock_qty: 2,
          norm_hours: 1.5,
          norm_hours_total: 1.5,
          stage_id: 10,
          stage_name: 'Сборка',
        }],
      }],
    },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <StageDistributionPage />
    </MemoryRouter>,
  )
}

describe('StageDistributionPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(calculateResourceDistribution).mockResolvedValue(distribution)
  })

  it('starts with an explicit empty state and calculation controls', () => {
    renderPage()

    expect(screen.getByRole('heading', { name: 'Распределение этапов' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Рассчитать' })).toBeEnabled()
    expect(screen.getByRole('checkbox', { name: 'Суммировать одинаковые детали' })).toBeChecked()
    expect(screen.getByText('Нажмите «Рассчитать», чтобы получить распределение')).toBeVisible()
    expect(calculateResourceDistribution).not.toHaveBeenCalled()
  })

  it('calculates resources, aggregates duplicate components and switches resource tabs', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(screen.getByRole('button', { name: 'Рассчитать' }))

    await waitFor(() => expect(calculateResourceDistribution).toHaveBeenCalledOnce())
    expect(await screen.findByText('Распределение рассчитано')).toBeVisible()
    expect(document.querySelector('.runBadge')).toHaveTextContent('Остатки: 20.07.2026 12:00')
    expect(screen.getByRole('button', { name: 'Механический участок · 7 н/ч' })).toHaveClass('activeTab')

    let shaftRows = screen.getAllByRole('row').filter((row) => within(row).queryByText('Вал ведущий'))
    expect(shaftRows).toHaveLength(1)
    expect(within(shaftRows[0]).getAllByRole('cell')[3]).toHaveTextContent('5')
    expect(within(shaftRows[0]).getAllByRole('cell')[6]).toHaveTextContent('5')

    await user.click(screen.getByRole('checkbox', { name: 'Суммировать одинаковые детали' }))
    shaftRows = screen.getAllByRole('row').filter((row) => within(row).queryByText('Вал ведущий'))
    expect(shaftRows).toHaveLength(2)

    await user.click(screen.getByRole('button', { name: 'Сборочный участок · 1,5 н/ч' }))
    expect(screen.getByText('Корпус насоса')).toBeVisible()
    expect(screen.queryByText('Вал ведущий')).not.toBeInTheDocument()
  })

  it('shows a failed calculation and allows a successful retry', async () => {
    const user = userEvent.setup()
    vi.mocked(calculateResourceDistribution)
      .mockRejectedValueOnce(new Error('Расчёт временно недоступен'))
      .mockResolvedValueOnce(distribution)
    renderPage()

    await user.click(screen.getByRole('button', { name: 'Рассчитать' }))
    expect(await screen.findByText('Расчёт временно недоступен')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Рассчитать' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: 'Рассчитать' }))
    expect(await screen.findByText('Распределение рассчитано')).toBeVisible()
    expect(calculateResourceDistribution).toHaveBeenCalledTimes(2)
  })
})
