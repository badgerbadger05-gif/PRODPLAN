import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  DbrBoard,
  DbrBuildResult,
  DbrProgram,
  DbrReleaseDayResult,
  DbrReleaseResult,
} from '../../domain/dbr'
import {
  activateDbrDrum,
  buildDbrDrum,
  getDbrBoard,
  listDbrPrograms,
  moveDbrSlot,
  refreshDbrGate,
  releaseDbrDay,
  releaseDbrSlot,
  rollForwardDbrDrum,
} from '../../services/dbr'
import { DbrDrumBoardPage } from './DbrDrumBoardPage'

vi.mock('../../services/dbr', () => ({
  activateDbrDrum: vi.fn(),
  buildDbrDrum: vi.fn(),
  getDbrBoard: vi.fn(),
  listDbrPrograms: vi.fn(),
  moveDbrSlot: vi.fn(),
  refreshDbrGate: vi.fn(),
  releaseDbrDay: vi.fn(),
  releaseDbrSlot: vi.fn(),
  rollForwardDbrDrum: vi.fn(),
}))

const board: DbrBoard = {
  schedule: {
    id: 7,
    period_from: '2026-07-20',
    period_to: '2026-07-31',
    status: 'active',
  },
  days: ['2026-07-20', '2026-07-21'],
  resources: [{ id: 11, name: 'Сборка' }, { id: 12, name: 'Испытания' }],
  slots: [{
    id: 101,
    date: '2026-07-20',
    resource_id: 11,
    resource_name: 'Сборка',
    item_id: 501,
    item_code: 'PUMP-01',
    item_name: 'Насос ГА-1',
    qty: 10,
    produced_qty: 2,
    kit_status: 'green',
    release_status: 'pending',
    shortage: [],
    position: 1,
  }, {
    id: 102,
    date: '2026-07-21',
    resource_id: 11,
    resource_name: 'Сборка',
    item_id: 502,
    item_code: 'GEAR-01',
    item_name: 'Редуктор',
    qty: 4,
    produced_qty: 0,
    kit_status: 'red',
    release_status: 'pending',
    shortage: [{ item: 'Подшипник', required: 4, available: 1, warehouse: 'Основной' }],
    position: 2,
  }],
  gaps: [{
    id: 301,
    date: '2026-07-21',
    resource_id: 11,
    resource_name: 'Сборка',
    item_id: 502,
    item_code: 'GEAR-01',
    item_name: 'Редуктор',
    required_qty: 4,
    takt_qty: 3,
    gap_qty: 1,
  }],
  kpi: { green: 1, yellow: 0, red: 1, unknown: 0, slots: 2, plan_qty: 14, fact_qty: 2 },
  calendar_fallback: true,
}

const program: DbrProgram = {
  id: 25,
  title: 'Июльская программа',
  from_date: '2026-07-20',
  to_date: '2026-07-31',
  status: 'approved',
  items: [],
}

const releasePreview: DbrReleaseResult = {
  ok: true,
  dry_run: true,
  kind: 'drum_slot',
  slot_id: 101,
  entity: 'Document_ЗаказНаПроизводство2_5',
  number: 'PREVIEW-101',
  payload: { Item_Key: 'ITEM-501' },
  created: false,
}

const releaseResult: DbrReleaseResult = {
  ...releasePreview,
  dry_run: false,
  number: 'ERP-101',
  created: true,
  one_c_order_ref: 'order-ref-101',
}

const dayPreview: DbrReleaseDayResult = {
  ok: true,
  dry_run: true,
  schedule_id: 7,
  day: '2026-07-20',
  slots_total: 1,
  released: 0,
  previews: 1,
  errors: 0,
  results: [releasePreview],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DbrDrumBoardPage />
    </MemoryRouter>,
  )
}

describe('DbrDrumBoardPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getDbrBoard).mockResolvedValue(board)
    vi.mocked(listDbrPrograms).mockResolvedValue([program])
    vi.mocked(refreshDbrGate).mockResolvedValue({ updated: 2, green: 1, yellow: 0, red: 1 })
    vi.mocked(rollForwardDbrDrum).mockResolvedValue({ moved: 2, closed: 1, overloaded: 1 })
    vi.mocked(moveDbrSlot).mockResolvedValue({ ok: true, moved: true, from: '2026-07-20', to: '2026-07-21' })
    vi.mocked(releaseDbrSlot).mockImplementation(async (_id, dryRun) => (
      dryRun ? releasePreview : releaseResult
    ))
    vi.mocked(releaseDbrDay).mockImplementation(async (_id, _day, dryRun) => (
      dryRun ? dayPreview : { ...dayPreview, dry_run: false, released: 1, previews: 0, results: [releaseResult] }
    ))
    const built: DbrBuildResult = {
      schedule: { ...board.schedule!, id: 8 },
      slots_added: 2,
      carried_over: [{ slot_id: 99 }],
    }
    vi.mocked(buildDbrDrum).mockResolvedValue(built)
    vi.mocked(activateDbrDrum).mockResolvedValue(built.schedule)
  })

  it('loads the active board with KPIs, capacity gaps and fallback warning', async () => {
    renderPage()

    expect(await screen.findByText('Насос ГА-1')).toBeInTheDocument()
    expect(screen.getByText('График №7 · active')).toBeInTheDocument()
    expect(screen.getByText(/Календарь работ не покрывает/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Разрывы мощности' })).toBeInTheDocument()
    expect(screen.getAllByText('Редуктор')).toHaveLength(2)
    expect(getDbrBoard).toHaveBeenCalledWith(expect.objectContaining({
      date_from: expect.any(String),
      date_to: expect.any(String),
    }))
  })

  it('refreshes the gate and rolls unfinished slots forward, reloading after each command', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'Обновить гейт' }))
    expect(await screen.findByText('Гейт обновлён: 🟢 1 · 🟡 0 · 🔴 1 (изменено 2)')).toBeInTheDocument()
    expect(refreshDbrGate).toHaveBeenCalledWith(7)

    await user.click(screen.getByRole('button', { name: 'Перенести невыполненное' }))
    expect(await screen.findByText('Перенесено плиток: 2 · закрыто выпуском: 1 · с перегрузом: 1')).toBeInTheDocument()
    expect(rollForwardDbrDrum).toHaveBeenCalledWith(7)
    expect(getDbrBoard).toHaveBeenCalledTimes(3)
  })

  it('builds an approved program, activates the resulting schedule and reloads the board', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'Построить из программы…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Построить график из программы' })
    const programSelect = within(dialog).getByLabelText('Утверждённая программа')
    expect(programSelect).toHaveFocus()
    expect(within(dialog).getByRole('option', { name: /№25 · Июльская программа/ })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Построить и активировать' }))

    expect(buildDbrDrum).toHaveBeenCalledWith(25)
    expect(activateDbrDrum).toHaveBeenCalledWith(8)
    expect(await screen.findByText(/График №8 построен и активирован · перенесено на след. период: 1/)).toBeInTheDocument()
    expect(getDbrBoard).toHaveBeenCalledTimes(2)
  })

  it('moves a selected slot to another date and resource', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /2\/10 Насос ГА-1/ }))

    const dialog = screen.getByRole('dialog', { name: 'Плитка: Насос ГА-1' })
    const date = within(dialog).getByLabelText('Перенести на дату')
    const resource = within(dialog).getByLabelText('Участок')
    expect(date).toHaveFocus()
    await user.clear(date)
    await user.type(date, '2026-07-21')
    await user.selectOptions(resource, '12')
    await user.click(within(dialog).getByRole('button', { name: 'Перенести' }))

    expect(moveDbrSlot).toHaveBeenCalledWith(101, '2026-07-21', 12)
    expect(await screen.findByText('Плитка перенесена на 21.07.2026')).toBeInTheDocument()
  })

  it('requires a dry-run preview before releasing a green slot to live 1С', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /2\/10 Насос ГА-1/ }))
    await user.click(screen.getByRole('button', { name: 'Релиз…' }))

    const confirm = await screen.findByRole('dialog', { name: 'Релиз плитки — Насос ГА-1' })
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(releaseDbrSlot).toHaveBeenNthCalledWith(1, 101, true)
    expect(within(confirm).getByText(/Будет создан документ в живой 1С/)).toBeInTheDocument()
    expect(within(confirm).getByText(/PREVIEW-101/)).toBeInTheDocument()

    await user.click(within(confirm).getByRole('button', { name: 'Провести в 1С' }))
    expect(releaseDbrSlot).toHaveBeenNthCalledWith(2, 101, false)
    expect(await within(confirm).findByText(/Заказ создан в 1С:/)).toBeInTheDocument()
    expect(within(confirm).getByText(/ERP-101/)).toBeInTheDocument()
  })

  it('runs day release as preview then confirmation and surfaces command errors', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'Релиз дня…' }))
    const dialog = screen.getByRole('dialog', { name: 'Релиз дня' })
    const date = within(dialog).getByLabelText('День для релиза')
    expect(date).toHaveFocus()
    await user.clear(date)
    await user.type(date, '2026-07-20')
    await user.click(within(dialog).getByRole('button', { name: 'Предпросмотр' }))

    expect(releaseDbrDay).toHaveBeenNthCalledWith(1, 7, '2026-07-20', true)
    expect(await within(dialog).findByText(/к релизу: 1/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Провести в 1С' }))
    expect(releaseDbrDay).toHaveBeenNthCalledWith(2, 7, '2026-07-20', false)
    expect(await within(dialog).findByText(/создано заказов: 1/)).toBeInTheDocument()

    vi.mocked(refreshDbrGate).mockRejectedValueOnce(new Error('Гейт временно недоступен'))
    await user.click(within(dialog).getByRole('button', { name: 'Закрыть' }))
    await user.click(screen.getByRole('button', { name: 'Обновить гейт' }))
    expect(await screen.findByText('Гейт временно недоступен')).toBeInTheDocument()
  })

  it('shows the board load failure without leaving the page in a busy state', async () => {
    vi.mocked(getDbrBoard).mockRejectedValueOnce(new Error('Не удалось загрузить барабан'))
    renderPage()

    expect(await screen.findByText('Не удалось загрузить барабан')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Обновить' })).toBeEnabled())
    expect(screen.getByText('Нет активного графика')).toBeInTheDocument()
  })
})
