import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TruthBadge } from './TruthBadge'
import { ApiError } from '../../lib/api'
import { truthBadgeMetaFromApiError } from '../../services/planningTruth'


describe('TruthBadge', () => {
  it('renders accepted generation identity', () => {
    render(<TruthBadge meta={{
      ledger_generation: 12,
      cutoff: '2026-08-02T08:00:00Z',
      truth_status: 'accepted',
      truth_reason: null,
    }} />)
    expect(screen.getByRole('status')).toHaveClass('accepted')
    expect(screen.getByText(/Истина принята · Ledger 12/)).toBeVisible()
  })

  it('treats unknown status as unavailable', () => {
    render(<TruthBadge meta={{
      ledger_generation: 12,
      cutoff: '2026-08-02T08:00:00Z',
      truth_status: 'future-status',
      truth_reason: 'unknown contract',
    }} />)
    expect(screen.getByRole('status')).toHaveClass('unavailable')
    expect(screen.getByText(/Истина недоступна: future-status/)).toBeVisible()
  })

  it('renders stale truth distinctly', () => {
    render(<TruthBadge meta={{
      ledger_generation: 12,
      cutoff: '2026-08-02T08:00:00Z',
      truth_status: 'stale',
      truth_reason: 'refresh required',
    }} />)
    expect(screen.getByRole('status')).toHaveClass('unavailable')
    expect(screen.getByText(/Истина устарела · Ledger 12/)).toBeVisible()
  })

  it('does not claim business unavailability when no truth response was received', () => {
    render(<TruthBadge meta={null} />)
    expect(screen.getByRole('status')).toHaveClass('neutral')
    expect(screen.getByText(/Статус истины не получен/)).toBeVisible()
    expect(screen.queryByText(/Истина недоступна/)).not.toBeInTheDocument()
  })

  it('accepts only the structured planning-truth 503 contract', () => {
    expect(truthBadgeMetaFromApiError(new ApiError('gateway', 503, {
      code: 'upstream_failed',
    }))).toBeNull()
    expect(truthBadgeMetaFromApiError(new ApiError('truth', 503, {
      code: 'planning_truth_unavailable',
      truth_status: 'stale',
      ledger_generation: 7,
      cutoff: '2026-08-02T08:00:00Z',
      reason: 'refresh required',
    }))).toEqual({
      truth_status: 'stale',
      truth_reason: 'refresh required',
      ledger_generation: 7,
      cutoff: '2026-08-02T08:00:00Z',
    })
  })
})
