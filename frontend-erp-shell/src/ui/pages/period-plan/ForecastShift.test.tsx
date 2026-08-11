import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ForecastShift } from './ForecastShift'


describe('ForecastShift', () => {
  it('maps backend forecast status without reclassifying shift days', () => {
    render(<ForecastShift forecast={{
      forecast_date: '2026-08-08',
      forecast_shift_days: 7,
      forecast_reason: 'смещение по мощностям',
      forecast_status: 'delayed',
    }} />)

    expect(screen.getByText(/\+7 дн/)).toHaveClass('warn')
    expect(screen.getByText(/\+7 дн/)).not.toHaveClass('late')
  })

  it('fails closed when status is absent', () => {
    render(<ForecastShift forecast={{ forecast_shift_days: 2 }} />)
    expect(screen.getByText(/\+2 дн/)).toHaveClass('unavailable')
  })
})
