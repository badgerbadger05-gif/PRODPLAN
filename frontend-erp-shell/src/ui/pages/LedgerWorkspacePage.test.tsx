import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  mockLedgerDataProvider,
  type LedgerDataProvider,
} from '../../services/ledger'
import { LedgerWorkspacePage } from './LedgerWorkspacePage'

describe('LedgerWorkspacePage', () => {
  it('connects journal, immutable detail, provenance and reversal chain', async () => {
    render(<LedgerWorkspacePage />)

    expect(await screen.findByText('P-1042')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('row', { name: /Сторно резерва/ }))
    expect(await screen.findByText('Команда сторнирования')).toBeInTheDocument()
    expect(screen.getByText(/Исходная проводка P-1043/)).toBeInTheDocument()
    expect(screen.getByText(/Производное значение доступно только для чтения/)).toBeInTheDocument()
  })

  it('submits filters through the replaceable provider and shows reconciliation', async () => {
    const provider: LedgerDataProvider = {
      loadSnapshot: vi.fn(mockLedgerDataProvider.loadSnapshot),
      loadPosting: vi.fn(mockLedgerDataProvider.loadPosting),
    }
    render(<LedgerWorkspacePage provider={provider} />)
    await screen.findByText('P-1042')

    fireEvent.change(screen.getByLabelText('Поиск проводок'), { target: { value: '943' } })
    fireEvent.click(screen.getByRole('button', { name: 'Найти' }))
    await waitFor(() => expect(provider.loadSnapshot).toHaveBeenLastCalledWith(
      expect.objectContaining({ search: '943' }),
      expect.any(AbortSignal),
    ))

    fireEvent.click(screen.getByRole('button', { name: /Сверка/ }))
    expect(await screen.findByText('Подшипник ведущего вала')).toBeInTheDocument()
    expect(screen.getByText(/всю каноническую область/)).toBeInTheDocument()
  })
})
