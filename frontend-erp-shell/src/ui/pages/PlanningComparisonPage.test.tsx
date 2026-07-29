import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  capturePlanningComparison,
  getPlanningComparisonBatch,
  listPlanningComparisonBatches,
} from '../../services/planningComparison'
import { PlanningComparisonPage } from './PlanningComparisonPage'

vi.mock('../../services/planningComparison', () => ({
  capturePlanningComparison: vi.fn(),
  getPlanningComparisonBatch: vi.fn(),
  listPlanningComparisonBatches: vi.fn(),
}))

const detail = {
  id: 7,
  capture_key: 'capture-7',
  created_at: '2026-07-23T12:00:00Z',
  cutoff_grade: 'near' as const,
  cutoff_reason: 'maximum input watermark skew is 2.000s',
  stable_run_key: 'stable-1',
  shadow_run_key: 'shadow-1',
  metrics: {
    rows: 2,
    by_kind: {
      production: { equal: 1, changed: 1, stable_only: 0, shadow_only: 0, absolute_delta: '3' },
    },
  },
  diffs: [
    {
      result_kind: 'production',
      canonical_key: 'production:item-a',
      item_key: 'item-a',
      stable_quantity: '10',
      shadow_quantity: '13',
      delta_quantity: '3',
      classification: 'changed' as const,
    },
    {
      result_kind: 'production',
      canonical_key: 'production:item-b',
      item_key: 'item-b',
      stable_quantity: '5',
      shadow_quantity: '5',
      delta_quantity: '0',
      classification: 'equal' as const,
    },
  ],
}

describe('PlanningComparisonPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listPlanningComparisonBatches).mockResolvedValue({
      rows: [detail],
      total: 1,
      limit: 50,
      offset: 0,
    })
    vi.mocked(getPlanningComparisonBatch).mockResolvedValue(detail)
    vi.mocked(capturePlanningComparison).mockResolvedValue(detail)
  })

  it('shows cutoff quality, metrics, diffs and the read-only capture guarantee', async () => {
    render(<PlanningComparisonPage />)

    expect((await screen.findAllByText('Близкий срез')).length).toBeGreaterThan(0)
    expect(screen.getByText(/не запускает планирование и ничего не записывает в 1С/i)).toBeInTheDocument()
    expect(screen.getByText('Σ отклонений: 3')).toBeInTheDocument()
    expect(screen.getByText('item-a')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Показать'), { target: { value: 'changed' } })
    expect(screen.getByText('item-a')).toBeInTheDocument()
    expect(screen.queryByText('item-b')).not.toBeInTheDocument()
  })

  it('takes a manual comparison without asking the UI to start planning', async () => {
    render(<PlanningComparisonPage />)
    const button = await screen.findByRole('button', { name: 'Снять сравнение' })
    fireEvent.click(button)

    await waitFor(() => expect(capturePlanningComparison).toHaveBeenCalledWith())
    expect(listPlanningComparisonBatches).toHaveBeenCalledTimes(2)
  })
})
