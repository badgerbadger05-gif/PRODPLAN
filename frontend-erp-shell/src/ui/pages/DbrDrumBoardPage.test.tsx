import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DbrBoard } from '../../domain/dbr'
import { ApiError } from '../../lib/api'
import { getDbrBoard } from '../../services/dbr'
import { DbrDrumBoardPage } from './DbrDrumBoardPage'

vi.mock('../../services/dbr', () => ({
  getDbrBoard: vi.fn(),
  dbrSnapshotUnavailableMessage: (error: unknown) => {
    const candidate = error as { status?: number; detail?: { code?: string; reason?: string }; message?: string }
    return candidate?.status === 503 && candidate.detail ? `${candidate.detail.code ?? 'snapshot_unavailable'}: ${candidate.detail.reason ?? candidate.message ?? ''}` : null
  },
}))

const board: DbrBoard = {
  meta: { snapshot_id: 83, ledger_generation: 42, cutoff: '2026-07-23T10:00:00Z', runs: [{ run_id: 17, freeze_version: 9 }, { run_id: 18, freeze_version: 4 }], read_only: true, unavailable_sections: ['kit_gate', 'execution'] },
  schedule: { id: 7, period_from: '2026-07-20', period_to: '2026-07-31', status: 'active' },
  days: ['2026-07-20', '2026-07-21'], resources: [{ id: 11, name: 'Сборка' }],
  slots: [{ id: 101, date: '2026-07-20', resource_id: 11, resource_name: 'Сборка', item_id: 501, item_code: 'PUMP-01', item_name: 'Насос ГА-1', qty: 10, produced_qty: null, kit_status: 'unknown', kit_gate_status: 'unavailable', execution_status: 'unavailable', release_status: 'pending', shortage: [], position: 1 }],
  gaps: [{ id: 301, date: '2026-07-21', resource_id: 11, resource_name: 'Сборка', item_id: 502, item_code: 'GEAR-01', item_name: 'Редуктор', required_qty: 4, takt_qty: 3, gap_qty: 1 }],
  kpi: { green: null, yellow: null, red: null, unknown: null, slots: 1, plan_qty: 10, fact_qty: null, kit_gate_status: 'unavailable', execution_status: 'unavailable' }, calendar_fallback: true,
}

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((r) => { resolve = r }); return { promise, resolve } }
function renderPage() { return render(<MemoryRouter><DbrDrumBoardPage /></MemoryRouter>) }

describe('DbrDrumBoardPage saved-snapshot characterization', () => {
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(getDbrBoard).mockResolvedValue(board) })
  it('boots exactly one saved board GET with no date query calculation', async () => { renderPage(); expect(await screen.findByText('Насос ГА-1')).toBeVisible(); expect(getDbrBoard).toHaveBeenCalledTimes(1); expect(getDbrBoard).toHaveBeenCalledWith() })
  it('shows saved ledger and all frozen MRP lineage', async () => { renderPage(); const line = await screen.findByTestId('drum-snapshot-lineage'); expect(line).toHaveTextContent('Снимок #83'); expect(line).toHaveTextContent('Ledger-поколение #42'); expect(line).toHaveTextContent('run #17'); expect(line).toHaveTextContent('run #18') })
  it('hides all mutable gate, move, roll-forward, release, and build controls', async () => { renderPage(); await screen.findByText('Насос ГА-1'); for (const name of ['Обновить гейт', 'Перенести невыполненное', 'Релиз дня…', 'Построить из программы…']) expect(screen.queryByRole('button', { name })).not.toBeInTheDocument(); await userEvent.setup().click(screen.getByRole('button', { name: /Насос ГА-1/ })); expect(screen.queryByRole('button', { name: 'Перенести' })).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: 'Релиз…' })).not.toBeInTheDocument() })
  it('renders unavailable gate and fact as unavailable rather than numeric zero', async () => { renderPage(); await screen.findByText('Насос ГА-1'); expect(screen.getByText(/гейт комплектности: unavailable/)).toBeVisible(); expect(screen.getByText(/факт выпуска: unavailable/)).toBeVisible(); expect(screen.getByText('— / 10')).toBeVisible() })
  it('keeps slot inspection local and read-only', async () => { const user = userEvent.setup(); renderPage(); await user.click(await screen.findByRole('button', { name: /Насос ГА-1/ })); const dialog = screen.getByRole('dialog', { name: 'Плитка: Насос ГА-1' }); expect(within(dialog).getByText((_, element) => element?.textContent === 'Комплектность: unavailable')).toBeVisible(); expect(within(dialog).getByText((_, element) => element?.textContent === 'Исполнение: unavailable')).toBeVisible(); expect(getDbrBoard).toHaveBeenCalledTimes(1) })
  it('renders capacity gaps and calendar warning from the snapshot', async () => { renderPage(); expect(await screen.findByRole('heading', { name: 'Разрывы мощности' })).toBeVisible(); expect(screen.getByText(/Календарь работ не покрывает/)).toBeVisible(); expect(screen.getByText('Редуктор')).toBeVisible() })
  it('renders structured 503 unavailable without fake board KPIs', async () => { vi.mocked(getDbrBoard).mockRejectedValue(new ApiError('not ready', 503, { code: 'dbr_drum_board_snapshot_unavailable', reason: 'no accepted generation' })); renderPage(); expect(await screen.findByRole('alert')).toHaveTextContent('dbr_drum_board_snapshot_unavailable: no accepted generation'); expect(screen.getByText('Сохранённый барабан недоступен')).toBeVisible(); expect(screen.queryByText('Комплектация')).not.toBeInTheDocument() })
  it('only refreshes the saved board after explicit refresh', async () => { const user = userEvent.setup(); renderPage(); await screen.findByText('Насос ГА-1'); await user.click(screen.getByRole('button', { name: 'Обновить снимок' })); await waitFor(() => expect(getDbrBoard).toHaveBeenCalledTimes(2)) })
  it('announces initial snapshot loading', async () => { const pending = deferred<DbrBoard>(); vi.mocked(getDbrBoard).mockReturnValue(pending.promise); renderPage(); expect(screen.getByRole('status')).toHaveTextContent('Загрузка сохранённого барабана'); pending.resolve(board); expect(await screen.findByText('Насос ГА-1')).toBeVisible() })
})
