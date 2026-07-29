import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { listPlanningRuns } from '../../services/planning'
import { MrpRunsPage } from './MrpRunsPage'

vi.mock('../../services/planning', () => ({
  listPlanningRuns: vi.fn(),
}))

const mockedList = vi.mocked(listPlanningRuns)

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/mrp-runs']}>
      <Routes>
        <Route path="/mrp-runs" element={<MrpRunsPage />} />
        <Route path="/mrp-runs/:runId" element={<div>Результат MRP</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('MrpRunsPage Doctype migration', () => {
  beforeEach(() => {
    mockedList.mockResolvedValue({
      rows: [
        {
          run_id: 42,
          status: 'completed',
          started_at: '2026-07-20T10:00:00',
          finished_at: '2026-07-20T10:05:00',
          period_from: '2026-07-21',
          period_to: '2026-07-31',
          source_plan_id: 7,
          source_plan_name: 'Июль',
          requirement_count: 120,
          requirement_remaining_qty: 15,
          order_count: 18,
          purchase_count: 6,
          overload_buckets: 2,
        },
      ],
      total: 1,
      limit: 30,
      offset: 0,
    })
  })

  it('preserves the dense list and summary detail', async () => {
    renderPage()

    expect(await screen.findByText('#42')).toBeInTheDocument()
    expect(screen.getAllByText('Июль')).toHaveLength(2)
    expect(screen.getAllByText('21.07.2026 — 31.07.2026')).toHaveLength(2)
    expect(screen.getByText('Строки 1-1 из 1')).toBeInTheDocument()
    expect(mockedList).toHaveBeenCalledWith({ limit: 30, offset: 0 })
  })

  it('opens the existing result route from the detail action', async () => {
    renderPage()
    await screen.findByText('#42')

    fireEvent.click(screen.getByRole('button', { name: 'Открыть результат' }))

    await waitFor(() => expect(screen.getByText('Результат MRP')).toBeInTheDocument())
  })
})
