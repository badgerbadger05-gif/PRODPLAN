import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DbrProgram } from '../../domain/dbr'
import {
  approveDbrProgram,
  createDbrProgram,
  getDbrProgram,
  listDbrPrograms,
  updateDbrProgram,
} from '../../services/dbr'
import { DbrProgramsPage } from './DbrProgramsPage'

vi.mock('../../services/dbr', () => ({
  approveDbrProgram: vi.fn(),
  createDbrProgram: vi.fn(),
  getDbrProgram: vi.fn(),
  listDbrPrograms: vi.fn(),
  updateDbrProgram: vi.fn(),
}))

vi.mock('../dbr/ItemPicker', () => ({
  ItemPicker: ({ onChange }: { onChange: (item: unknown) => void }) => (
    <button type="button" onClick={() => onChange({ item_id: 77, item_code: 'ITEM-77', item_name: 'Корпус' })}>
      Выбрать корпус
    </button>
  ),
}))

const draft: DbrProgram = {
  id: 11,
  title: 'Июльский план',
  company: 'ЗСМ',
  from_date: '2026-07-01',
  to_date: '2026-07-31',
  status: 'draft',
  items: [{
    id: 101,
    item_id: 77,
    item_code: 'ITEM-77',
    item_name: 'Корпус',
    program_date: '2026-07-10',
    qty: 12,
    comment: 'Первая партия',
  }],
}

const approved: DbrProgram = { ...draft, status: 'approved' }
const approvedList: DbrProgram = { ...approved, id: 12, title: 'Августовский план' }

function renderPage() {
  return render(<MemoryRouter><DbrProgramsPage /></MemoryRouter>)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

describe('DbrProgramsPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listDbrPrograms).mockResolvedValue([draft, approvedList])
    vi.mocked(getDbrProgram).mockResolvedValue(draft)
    vi.mocked(createDbrProgram).mockResolvedValue(draft)
    vi.mocked(updateDbrProgram).mockResolvedValue(draft)
    vi.mocked(approveDbrProgram).mockResolvedValue(approved)
  })

  it('loads and refreshes the program list with status and company metadata', async () => {
    renderPage()

    expect(await screen.findByText('Июльский план')).toBeInTheDocument()
    expect(screen.getByText('Программ: 2')).toBeInTheDocument()
    expect(screen.getAllByText('ЗСМ')).toHaveLength(2)
    expect(screen.getAllByText('Черновик')).toHaveLength(1)
    expect(screen.getAllByText('Утверждена')).toHaveLength(1)

    await userEvent.click(screen.getByRole('button', { name: 'Обновить' }))
    await waitFor(() => expect(listDbrPrograms).toHaveBeenCalledTimes(2))
  })

  it('opens a program and exposes its draft rows for editing', async () => {
    renderPage()
    const title = await screen.findByText('Июльский план')

    await userEvent.click(title)

    expect(getDbrProgram).toHaveBeenCalledWith(11)
    expect(await screen.findByText('Программа №11: Июльский план')).toBeInTheDocument()
    expect(screen.getByDisplayValue('12')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Первая партия')).toBeInTheDocument()
  })

  it('validates a new program and sends normalized optional fields and items', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Июльский план')

    await user.click(screen.getByRole('button', { name: 'Создать программу' }))
    expect(screen.getByText('В каждой строке укажите номенклатуру, дату и количество больше нуля')).toBeInTheDocument()

    await user.clear(screen.getByLabelText('Период с'))
    await user.type(screen.getByLabelText('Период с'), '2026-07-01')
    await user.clear(screen.getByLabelText('Период по'))
    await user.type(screen.getByLabelText('Период по'), '2026-07-31')
    await user.type(screen.getByLabelText('Название'), '  Июльский план  ')
    await user.type(screen.getByLabelText('Компания'), '  ЗСМ  ')
    await user.click(screen.getAllByRole('button', { name: 'Выбрать корпус' })[0])

    const createSection = screen.getByRole('heading', { name: 'Новая программа' }).closest('section')
    if (!createSection) throw new Error('new program section not found')
    const rowDate = createSection.querySelectorAll<HTMLInputElement>('input[type="date"]')[2]
    fireEvent.change(rowDate, { target: { value: '2026-07-01' } })
    fireEvent.change(within(createSection).getByPlaceholderText('шт'), { target: { value: '25.5' } })
    fireEvent.change(within(createSection).getAllByPlaceholderText('необязательно')[1], { target: { value: '  срочно  ' } })
    await user.click(screen.getByRole('button', { name: 'Создать программу' }))

    await waitFor(() => expect(createDbrProgram).toHaveBeenCalledWith({
      from_date: '2026-07-01',
      to_date: '2026-07-31',
      title: 'Июльский план',
      company: 'ЗСМ',
      items: [{ item_id: 77, program_date: '2026-07-01', qty: 25.5, comment: 'срочно' }],
    }))
    expect(await screen.findByText('Программа №11 создана (1 строк)')).toBeInTheDocument()
    expect(listDbrPrograms).toHaveBeenCalledTimes(2)
  })

  it('keeps the first new row date aligned with period start until that date is edited', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Июльский план')
    const section = screen.getByRole('heading', { name: 'Новая программа' }).closest('section')
    if (!section) throw new Error('new program section not found')
    const dates = section.querySelectorAll<HTMLInputElement>('input[type="date"]')

    fireEvent.change(screen.getByLabelText('Период с'), { target: { value: '2026-08-01' } })
    expect(dates[2]).toHaveValue('2026-08-01')

    fireEvent.change(dates[2], { target: { value: '2026-08-03' } })
    fireEvent.change(screen.getByLabelText('Период с'), { target: { value: '2026-08-02' } })
    expect(dates[2]).toHaveValue('2026-08-03')
    await user.click(screen.getByRole('button', { name: 'Добавить строку' }))
    expect(section.querySelectorAll<HTMLInputElement>('input[type="date"]')[3]).toHaveValue('2026-08-02')
  })

  it('updates draft items and refreshes the list', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByText('Июльский план'))
    const detail = await screen.findByRole('heading', { name: 'Программа №11: Июльский план' })
    const section = detail.closest('section')
    if (!section) throw new Error('program detail section not found')

    fireEvent.change(within(section).getByDisplayValue('12'), { target: { value: '18' } })
    fireEvent.change(within(section).getByDisplayValue('Первая партия'), { target: { value: '  обновлено  ' } })
    await user.click(within(section).getByRole('button', { name: 'Сохранить строки' }))

    await waitFor(() => expect(updateDbrProgram).toHaveBeenCalledWith(11, {
      items: [{ item_id: 77, program_date: '2026-07-10', qty: 18, comment: 'обновлено' }],
    }))
    expect(await screen.findByText('Строки программы №11 сохранены')).toBeInTheDocument()
    expect(listDbrPrograms).toHaveBeenCalledTimes(2)
  })

  it('approves a draft from the list without opening it and refreshes programs', async () => {
    const user = userEvent.setup()
    renderPage()
    const title = await screen.findByText('Июльский план')
    const row = title.closest('tr')
    if (!row) throw new Error('program row not found')

    await user.click(within(row).getByRole('button', { name: 'Утвердить' }))

    expect(approveDbrProgram).toHaveBeenCalledWith(11)
    expect(getDbrProgram).not.toHaveBeenCalled()
    expect(await screen.findByText('Программа №11 утверждена')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Программа №11: Июльский план' })).toBeInTheDocument()
    expect(listDbrPrograms).toHaveBeenCalledTimes(2)
  })

  it('surfaces load, detail, create, update, and approve service errors', async () => {
    vi.mocked(listDbrPrograms).mockRejectedValueOnce(new Error('list unavailable'))
    renderPage()
    expect(await screen.findByText('list unavailable')).toBeInTheDocument()

    vi.mocked(listDbrPrograms).mockResolvedValueOnce([draft])
    await userEvent.click(screen.getByRole('button', { name: 'Обновить' }))
    expect(await screen.findByText('Июльский план')).toBeInTheDocument()

    vi.mocked(getDbrProgram).mockRejectedValueOnce(new Error('detail unavailable'))
    await userEvent.click(screen.getByText('Июльский план'))
    expect(await screen.findByText('detail unavailable')).toBeInTheDocument()

    const row = screen.getByText('Июльский план').closest('tr')
    if (!row) throw new Error('program row not found')
    vi.mocked(approveDbrProgram).mockRejectedValueOnce(new Error('approve rejected'))
    await userEvent.click(within(row).getByRole('button', { name: 'Утвердить' }))
    expect(await screen.findByText('approve rejected')).toBeInTheDocument()
  })

  it('keeps the newest program detail when an older open request resolves last', async () => {
    const oldDetail = deferred<DbrProgram>()
    const newDetail = deferred<DbrProgram>()
    vi.mocked(getDbrProgram)
      .mockReturnValueOnce(oldDetail.promise)
      .mockReturnValueOnce(newDetail.promise)
    renderPage()
    await screen.findByText('Июльский план')

    fireEvent.click(screen.getByText('Июльский план'))
    fireEvent.click(screen.getByText('Августовский план'))
    await act(async () => { newDetail.resolve(approvedList) })
    expect(await screen.findByRole('heading', { name: 'Программа №12: Августовский план' })).toBeInTheDocument()

    await act(async () => { oldDetail.resolve(draft) })
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Программа №12: Августовский план' })).toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: 'Программа №11: Июльский план' })).not.toBeInTheDocument()
  })

  it('allows only one mutation while competing program commands are fired together', async () => {
    const pending = deferred<DbrProgram>()
    vi.mocked(approveDbrProgram).mockReturnValueOnce(pending.promise)
    renderPage()
    const title = await screen.findByText('Июльский план')
    const row = title.closest('tr')
    if (!row) throw new Error('program row not found')
    const approveButton = within(row).getByRole('button', { name: 'Утвердить' })

    act(() => {
      fireEvent.click(approveButton)
      fireEvent.click(approveButton)
      fireEvent.click(screen.getByRole('button', { name: 'Создать программу' }))
    })

    expect(approveDbrProgram).toHaveBeenCalledOnce()
    expect(createDbrProgram).not.toHaveBeenCalled()
    await act(async () => { pending.resolve(approved) })
    expect(await screen.findByText('Программа №11 утверждена')).toBeInTheDocument()
  })
})
