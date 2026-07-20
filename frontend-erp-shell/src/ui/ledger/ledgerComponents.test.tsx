import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { LedgerPostingTable } from './LedgerPostingTable'
import { ProvenanceTimeline } from './ProvenanceTimeline'
import { ReconciliationIssuesTable } from './ReconciliationIssuesTable'

describe('ledger presentation primitives', () => {
  it('renders immutable posting provenance and supports keyboard activation', () => {
    const activate = vi.fn()
    render(
      <LedgerPostingTable
        rows={[{
          id: 'posting-1',
          occurredAt: '2026-07-20T10:05:00',
          itemLabel: 'Корпус редуктора',
          poolKey: 'item:15|warehouse:7',
          eventType: 'Выпуск',
          quantityDelta: 4,
          unit: 'шт',
          sourceDocument: 'СборкаЗапасов ПТ-5',
          correlationId: 'cycle-42',
        }]}
        onActivate={activate}
      />,
    )

    expect(screen.getByText('+4')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('row', { name: /Выпуск/ }), { key: 'Enter' })
    expect(activate).toHaveBeenCalledOnce()
  })

  it('shows calculation provenance and reconciliation differences', () => {
    render(
      <>
        <ProvenanceTimeline steps={[{
          id: 'step-1',
          occurredAt: '2026-07-20T10:00:00',
          kind: 'source',
          title: 'Документ 1С прочитан',
          actor: 'sync-worker',
          correlationId: 'cycle-42',
        }]} />
        <ReconciliationIssuesTable rows={[{
          id: 'issue-1',
          severity: 'warning',
          poolKey: 'item:15|warehouse:7',
          itemLabel: 'Корпус редуктора',
          ledgerQuantity: 10,
          projectionQuantity: 8,
          difference: 2,
          status: 'open',
          detectedAt: '2026-07-20T10:10:00',
        }]} />
      </>,
    )

    expect(screen.getByText('Документ 1С прочитан')).toBeInTheDocument()
    expect(screen.getByText('+2')).toBeInTheDocument()
    expect(screen.getByText('Предупреждение')).toBeInTheDocument()
  })
})

