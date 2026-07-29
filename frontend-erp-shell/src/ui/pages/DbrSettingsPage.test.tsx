import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DbrAssemblyRate, DbrCategoryRisk, DbrSettings } from '../../domain/dbr'
import {
  deleteDbrAssemblyRate,
  getDbrSettings,
  listDbrAssemblyRates,
  listDbrCategoryRisks,
  replaceDbrCategoryRisks,
  updateDbrSettings,
  upsertDbrAssemblyRate,
} from '../../services/dbr'
import { listResources } from '../../services/resources'
import { DbrSettingsPage } from './DbrSettingsPage'

vi.mock('../../services/dbr', () => ({
  deleteDbrAssemblyRate: vi.fn(),
  getDbrSettings: vi.fn(),
  listDbrAssemblyRates: vi.fn(),
  listDbrCategoryRisks: vi.fn(),
  replaceDbrCategoryRisks: vi.fn(),
  updateDbrSettings: vi.fn(),
  upsertDbrAssemblyRate: vi.fn(),
}))

vi.mock('../../services/resources', () => ({
  listResources: vi.fn(),
}))

vi.mock('../dbr/ItemPicker', () => ({
  ItemPicker: ({ onChange }: { onChange: (item: unknown) => void }) => (
    <button
      type="button"
      onClick={() => onChange({ item_id: 77, item_code: 'ITEM-77', item_name: 'Корпус' })}
    >
      Выбрать корпус
    </button>
  ),
}))

const settings: DbrSettings = {
  id: 1,
  frozen_days: 3,
  gate_horizon_workdays: 5,
  shelf_threshold_qty: '12.5',
  rt_machining_days: 2,
  rt_welding_days: 3,
  rt_painting_days: 4,
  batch_days_turning: 1,
  batch_days_bending: 2,
  batch_days_welding: 3,
  batch_days_paint_black: 4,
  batch_days_paint_color: 5,
  feeder_chain_enabled: true,
  feeder_load_horizon_weeks: 6,
  w2_warehouse_ref1c: 'W2',
  w3_warehouse_ref1c: null,
  w4_warehouse_ref1c: 'W4',
  fastener_categories: ['Болты'],
}

const rate: DbrAssemblyRate = {
  id: 11,
  resource_id: 4,
  resource_name: 'Сборка',
  item_id: 77,
  item_code: 'ITEM-77',
  item_name: 'Корпус',
  qty_per_capacity: '8.5',
}

const risk: DbrCategoryRisk = {
  id: 21,
  item_group: 'Подшипники',
  receipt_warehouse_ref1c: 'RECEIPT',
  supply_risk_pct: '15',
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DbrSettingsPage />
    </MemoryRouter>,
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

describe('DbrSettingsPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getDbrSettings).mockResolvedValue(settings)
    vi.mocked(listDbrAssemblyRates).mockResolvedValue([rate])
    vi.mocked(listDbrCategoryRisks).mockResolvedValue([risk])
    vi.mocked(listResources).mockResolvedValue([{ resource_id: 4, resource_name: 'Сборка' }])
    vi.mocked(updateDbrSettings).mockResolvedValue(settings)
    vi.mocked(upsertDbrAssemblyRate).mockResolvedValue(rate)
    vi.mocked(deleteDbrAssemblyRate).mockResolvedValue({ deleted: 1 })
    vi.mocked(replaceDbrCategoryRisks).mockResolvedValue([risk])
  })

  it('loads the settings singleton, assembly rates, risks, and resources together', async () => {
    renderPage()

    expect(await screen.findByDisplayValue('12.5')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Сборка' })).toBeInTheDocument()
    expect(screen.getByText('ITEM-77 · ID 77')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Подшипники')).toBeInTheDocument()
    expect(screen.getByText('Тактов: 1 · Рисков: 1')).toBeInTheDocument()
    expect(getDbrSettings).toHaveBeenCalledOnce()
    expect(listDbrAssemblyRates).toHaveBeenCalledOnce()
    expect(listDbrCategoryRisks).toHaveBeenCalledOnce()
    expect(listResources).toHaveBeenCalledOnce()
  })

  it('normalizes warehouse refs, categories, and numeric settings on save', async () => {
    const user = userEvent.setup()
    vi.mocked(updateDbrSettings).mockImplementation(async (payload) => ({ ...settings, ...payload }))
    renderPage()
    await screen.findByDisplayValue('12.5')

    await user.clear(screen.getByLabelText('Склад №2'))
    await user.type(screen.getByLabelText('Склад №2'), '  W2-new  ')
    await user.clear(screen.getByLabelText('Склад №3'))
    await user.type(screen.getByLabelText('Склад №3'), '   ')
    await user.clear(screen.getByLabelText('Категории метизов (по одной в строке)'))
    await user.type(screen.getByLabelText('Категории метизов (по одной в строке)'), ' Болты \n\nГайки\nБолты')
    await user.click(screen.getByRole('button', { name: 'Сохранить настройки' }))

    await waitFor(() => expect(updateDbrSettings).toHaveBeenCalledWith(expect.objectContaining({
      shelf_threshold_qty: 12.5,
      w2_warehouse_ref1c: 'W2-new',
      w3_warehouse_ref1c: null,
      fastener_categories: ['Болты', 'Гайки'],
    })))
    expect(await screen.findByText('Настройки сохранены')).toBeInTheDocument()
  })

  it('shows an initial load error and retries all four reads on refresh', async () => {
    vi.mocked(getDbrSettings).mockRejectedValueOnce(new Error('settings unavailable'))
    renderPage()

    expect(await screen.findByText('settings unavailable')).toBeInTheDocument()
    expect(screen.getByText('Настройки недоступны')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Обновить' }))

    expect(await screen.findByDisplayValue('12.5')).toBeInTheDocument()
    expect(getDbrSettings).toHaveBeenCalledTimes(2)
    expect(listDbrAssemblyRates).toHaveBeenCalledTimes(2)
    expect(listDbrCategoryRisks).toHaveBeenCalledTimes(2)
    expect(listResources).toHaveBeenCalledTimes(2)
    expect(screen.queryByText('settings unavailable')).not.toBeInTheDocument()
  })

  it('reloads all settings reads with F5 from editable controls but not from interactive controls', async () => {
    renderPage()
    const field = await screen.findByLabelText('Порог полки, шт')

    fireEvent.keyDown(field, { key: 'F5' })
    await waitFor(() => expect(getDbrSettings).toHaveBeenCalledTimes(2))

    fireEvent.keyDown(screen.getByRole('button', { name: 'Сохранить настройки' }), { key: 'F5' })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(getDbrSettings).toHaveBeenCalledTimes(2)
    expect(listDbrAssemblyRates).toHaveBeenCalledTimes(2)
    expect(listDbrCategoryRisks).toHaveBeenCalledTimes(2)
    expect(listResources).toHaveBeenCalledTimes(2)
  })

  it('keeps successful settings sections when independent reads fail and retries accessibly', async () => {
    vi.mocked(listDbrAssemblyRates).mockRejectedValueOnce(new Error('rates unavailable'))
    vi.mocked(listResources).mockRejectedValueOnce(new Error('resources unavailable'))
    renderPage()

    expect(await screen.findByDisplayValue('12.5')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Подшипники')).toBeInTheDocument()
    expect(screen.getByText('Тактов: 0 · Рисков: 1')).toBeInTheDocument()
    const errors = screen.getByRole('alert', { name: 'Ошибки загрузки' })
    expect(within(errors).getByText('Такты сборки: rates unavailable')).toBeInTheDocument()
    expect(within(errors).getByText('Участки: resources unavailable')).toBeInTheDocument()

    await userEvent.click(within(errors).getByRole('button', { name: 'Повторить загрузку' }))

    expect(await screen.findByText('ITEM-77 · ID 77')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Сборка' })).toBeInTheDocument()
    expect(screen.queryByRole('alert', { name: 'Ошибки загрузки' })).not.toBeInTheDocument()
  })

  it('keeps the newest load when StrictMode starts two reads and the older one resolves last', async () => {
    const oldSettings = deferred<DbrSettings>()
    const newSettings = deferred<DbrSettings>()
    vi.mocked(getDbrSettings)
      .mockReturnValueOnce(oldSettings.promise)
      .mockReturnValueOnce(newSettings.promise)
    vi.mocked(listDbrAssemblyRates)
      .mockResolvedValueOnce([{ ...rate, id: 1, item_name: 'Старый такт' }])
      .mockResolvedValueOnce([{ ...rate, id: 2, item_name: 'Новый такт' }])

    render(
      <StrictMode>
        <MemoryRouter>
          <DbrSettingsPage />
        </MemoryRouter>
      </StrictMode>,
    )

    await waitFor(() => expect(getDbrSettings).toHaveBeenCalledTimes(2))
    await act(async () => {
      newSettings.resolve({ ...settings, shelf_threshold_qty: 99 })
    })
    expect(await screen.findByDisplayValue('99')).toBeInTheDocument()
    expect(screen.getByText('Новый такт')).toBeInTheDocument()

    await act(async () => {
      oldSettings.resolve({ ...settings, shelf_threshold_qty: 1 })
    })
    await waitFor(() => expect(screen.getByDisplayValue('99')).toBeInTheDocument())
    expect(screen.getByText('Новый такт')).toBeInTheDocument()
    expect(screen.queryByText('Старый такт')).not.toBeInTheDocument()
  })

  it('allows only one settings mutation while competing commands are fired together', async () => {
    const pendingSave = deferred<DbrSettings>()
    vi.mocked(updateDbrSettings).mockReturnValueOnce(pendingSave.promise)
    renderPage()
    await screen.findByDisplayValue('12.5')

    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки' }))
      fireEvent.click(screen.getAllByRole('button', { name: 'Удалить' })[0])
      fireEvent.click(screen.getByRole('button', { name: 'Сохранить риски' }))
    })

    expect(updateDbrSettings).toHaveBeenCalledOnce()
    expect(deleteDbrAssemblyRate).not.toHaveBeenCalled()
    expect(replaceDbrCategoryRisks).not.toHaveBeenCalled()

    await act(async () => {
      pendingSave.resolve(settings)
    })
    expect(await screen.findByText('Настройки сохранены')).toBeInTheDocument()
  })

  it('validates and saves an assembly rate, then refreshes the rate list', async () => {
    const user = userEvent.setup()
    vi.mocked(listDbrAssemblyRates)
      .mockResolvedValueOnce([rate])
      .mockResolvedValueOnce([{ ...rate, id: 12, qty_per_capacity: 9 }])
    renderPage()
    await screen.findByDisplayValue('12.5')

    await user.click(screen.getByRole('button', { name: 'Добавить' }))
    expect(screen.getByText('Укажите участок и номенклатуру')).toBeInTheDocument()

    await user.selectOptions(screen.getByRole('combobox'), '4')
    await user.click(screen.getByRole('button', { name: 'Выбрать корпус' }))
    const addRow = screen.getByRole('button', { name: 'Добавить' }).closest('tr')
    if (!addRow) throw new Error('assembly-rate add row not found')
    const qtyInput = within(addRow).getByPlaceholderText('шт/сутки')
    fireEvent.change(qtyInput, { target: { value: '0' } })
    await user.click(screen.getByRole('button', { name: 'Добавить' }))
    expect(screen.getByText('Такт сборки должен быть больше нуля')).toBeInTheDocument()

    fireEvent.change(qtyInput, { target: { value: '9' } })
    await user.click(screen.getByRole('button', { name: 'Добавить' }))

    await waitFor(() => expect(upsertDbrAssemblyRate).toHaveBeenCalledWith({
      resource_id: 4,
      item_id: 77,
      qty_per_capacity: 9,
    }))
    expect(listDbrAssemblyRates).toHaveBeenCalledTimes(2)
    expect(await screen.findByText('Такт сборки сохранён')).toBeInTheDocument()
  })

  it('removes a persisted rate locally and exposes a delete failure without removing it', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('ITEM-77 · ID 77')

    vi.mocked(deleteDbrAssemblyRate).mockRejectedValueOnce(new Error('delete denied'))
    await user.click(screen.getAllByRole('button', { name: 'Удалить' })[0])
    expect(await screen.findByText('delete denied')).toBeInTheDocument()
    expect(screen.getByText('ITEM-77 · ID 77')).toBeInTheDocument()

    await user.click(screen.getAllByRole('button', { name: 'Удалить' })[0])
    expect(await screen.findByText('Такт сборки удалён')).toBeInTheDocument()
    expect(screen.queryByText('ITEM-77 · ID 77')).not.toBeInTheDocument()
  })

  it('filters empty risk rows and normalizes optional values when replacing risks', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByDisplayValue('Подшипники')

    await user.click(screen.getByRole('button', { name: 'Добавить строку' }))
    const categoryInputs = screen.getAllByPlaceholderText('категория')
    await user.clear(categoryInputs[0])
    await user.type(categoryInputs[0], '  Электрика  ')
    await user.type(categoryInputs[1], '   ')
    const warehouseInputs = screen.getAllByPlaceholderText('ref 1С')
    await user.clear(warehouseInputs[0])
    await user.type(warehouseInputs[0], '   ')
    const riskInputs = screen.getAllByRole('spinbutton')
    const riskPercent = riskInputs[riskInputs.length - 2]
    await user.clear(riskPercent)

    await user.click(screen.getByRole('button', { name: 'Сохранить риски' }))

    await waitFor(() => expect(replaceDbrCategoryRisks).toHaveBeenCalledWith([{
      item_group: 'Электрика',
      receipt_warehouse_ref1c: null,
      supply_risk_pct: null,
    }]))
    expect(await screen.findByText('Категорийные риски сохранены')).toBeInTheDocument()
  })

  it('keeps edited risk rows and reports a replace failure', async () => {
    const user = userEvent.setup()
    vi.mocked(replaceDbrCategoryRisks).mockRejectedValueOnce(new Error('risk write failed'))
    renderPage()
    const input = await screen.findByDisplayValue('Подшипники')
    await user.clear(input)
    await user.type(input, 'Кабель')

    await user.click(screen.getByRole('button', { name: 'Сохранить риски' }))

    expect(await screen.findByText('risk write failed')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Кабель')).toBeInTheDocument()
  })
})
