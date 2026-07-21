import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DbrProgram, DbrPurchaseLaunchResult, DbrPurchasePlanPreview } from '../../domain/dbr'
import {
  listDbrPrograms,
  materializeDbrPurchasePlan,
  previewDbrPurchasePlan,
} from '../../services/dbr'
import { DbrPurchasePage } from './DbrPurchasePage'

vi.mock('../../services/dbr', () => ({
  listDbrPrograms: vi.fn(),
  materializeDbrPurchasePlan: vi.fn(),
  previewDbrPurchasePlan: vi.fn(),
}))

const program: DbrProgram = {
  id: 17,
  title: 'Июльский план',
  from_date: '2026-07-01',
  to_date: '2026-07-31',
  status: 'approved',
  items: [],
}

const activePreview: DbrPurchasePlanPreview = {
  ok: true,
  source: { kind: 'active' },
  lead_time_threshold_days: 60,
  rows_to_order: 2,
  items_total: 3,
  warnings: ['Для ITEM-2 не назначен поставщик'],
  rows: [
    {
      item_id: 1,
      item_code: 'ITEM-1',
      item_name: 'Подшипник',
      supplier_ref1c: 'SUPPLIER-1',
      demand_qty: 10,
      stock_qty: 2,
      open_order_qty: 1,
      available_qty: 3,
      to_order_qty: 7,
      need_date: '2026-08-20',
      replenishment_time: 30,
      order_before: '2026-07-21',
      within_lead_time_threshold: true,
    },
    {
      item_id: 2,
      item_code: 'ITEM-2',
      item_name: 'Редуктор',
      supplier_ref1c: null,
      demand_qty: 5,
      stock_qty: 0,
      open_order_qty: 0,
      available_qty: 0,
      to_order_qty: 5,
      need_date: '2026-10-01',
      replenishment_time: 20,
      order_before: '2026-09-11',
      within_lead_time_threshold: false,
    },
    {
      item_id: 3,
      item_code: 'ITEM-3',
      item_name: 'Крепёж',
      supplier_ref1c: 'SUPPLIER-2',
      demand_qty: 1,
      stock_qty: 4,
      open_order_qty: 0,
      available_qty: 4,
      to_order_qty: 0,
      replenishment_time: 5,
      order_before: null,
      within_lead_time_threshold: false,
    },
  ],
}

const dryRun: DbrPurchaseLaunchResult = {
  ok: true,
  dry_run: true,
  kind: 'purchase_plan',
  entity: 'Document_ЗаказПоставщику',
  source: { kind: 'active' },
  orders_planned: 1,
  items_total: 2,
  unresolved: [{ item_id: 2, item_name: 'Редуктор', missing_supplier: true, missing_item_ref1c: false }],
  already_exported: [],
  orders_created: 0,
  orders: [{
    supplier_ref1c: 'SUPPLIER-1',
    number: 'PREVIEW-1',
    lines: [{
      item_id: 1,
      item_ref1c: 'ITEM-REF-1',
      item_name: 'Подшипник',
      qty: 7,
      need_date: '2026-08-20',
      order_date: '2026-07-21',
      source_ids: [1],
    }],
  }],
}

const committed: DbrPurchaseLaunchResult = {
  ...dryRun,
  dry_run: false,
  orders_created: 1,
  orders: [{ ...dryRun.orders[0], number: 'PO-2026-001', status: 'created' }],
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

function renderPage() {
  return render(<MemoryRouter><DbrPurchasePage /></MemoryRouter>)
}

async function calculate(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Рассчитать' }))
  await screen.findByText('ITEM-1')
}

describe('DbrPurchasePage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listDbrPrograms).mockResolvedValue([program])
    vi.mocked(previewDbrPurchasePlan).mockResolvedValue(activePreview)
    vi.mocked(materializeDbrPurchasePlan)
      .mockResolvedValueOnce(dryRun)
      .mockResolvedValueOnce(committed)
  })

  it('bootstraps programs and calculates the active schedule with the selected horizon', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('option', { name: /Программа №17.*Июльский план/ })).toBeVisible()
    await user.clear(screen.getByRole('spinbutton', { name: 'Горизонт заказа, дней' }))
    await user.type(screen.getByRole('spinbutton', { name: 'Горизонт заказа, дней' }), '45')
    await calculate(user)

    expect(previewDbrPurchasePlan).toHaveBeenCalledWith({ active: true, thresholdDays: 45 })
    expect(screen.getByText('Позиций в плане').parentElement).toHaveTextContent('3')
    expect(screen.getByText('К заказу').parentElement).toHaveTextContent('2')
    expect(screen.getByText('В горизонте 60 дн.').parentElement).toHaveTextContent('1')
    expect(screen.getByText('Предупреждения качества: 1')).toBeVisible()
  })

  it('uses program source parameters and applies row filtering and sorting locally', async () => {
    const user = userEvent.setup()
    renderPage()
    const source = await screen.findByRole('combobox', { name: 'Источник' })
    await user.selectOptions(source, '17')
    await calculate(user)

    expect(previewDbrPurchasePlan).toHaveBeenCalledWith({ programId: 17, thresholdDays: 60 })
    expect(screen.queryByText('ITEM-3')).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Только к заказу' }))
    expect(screen.getByText('ITEM-3')).toBeVisible()

    const body = document.querySelector('.dbrPurchaseTable tbody')
    expect(body).not.toBeNull()
    await user.click(screen.getByText('Позиция'))
    expect(within(body as HTMLElement).getAllByRole('row').map((row) => row.textContent)).toEqual([
      expect.stringContaining('ITEM-1'),
      expect.stringContaining('ITEM-2'),
      expect.stringContaining('ITEM-3'),
    ])
  })

  it('clears the old result and presents a preview error without enabling materialization', async () => {
    const user = userEvent.setup()
    vi.mocked(previewDbrPurchasePlan)
      .mockResolvedValueOnce(activePreview)
      .mockRejectedValueOnce(new Error('Расчёт недоступен'))
    renderPage()
    await calculate(user)

    await user.click(screen.getByRole('button', { name: 'Рассчитать' }))
    expect(await screen.findByText('Расчёт недоступен')).toBeVisible()
    expect(screen.queryByText('ITEM-1')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Сформировать заказы…' })).toBeDisabled()
  })

  it('previews and confirms the exact source while refreshing the read model after the write', async () => {
    const user = userEvent.setup()
    renderPage()
    await calculate(user)

    await user.click(screen.getByRole('button', { name: 'Сформировать заказы…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Формирование заказов поставщику' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByText(/Будет создан документ в живой 1С/)).toBeVisible()
    expect(within(dialog).getByText('Документ № PREVIEW-1')).toBeVisible()
    expect(materializeDbrPurchasePlan).toHaveBeenNthCalledWith(1, {
      active: true, thresholdDays: 60, dryRun: true,
    })

    await user.click(within(dialog).getByRole('button', { name: 'Провести в 1С' }))
    expect(await within(dialog).findByText(/Документ проведён в живой 1С/)).toBeVisible()
    expect(within(dialog).getByText(/PO-2026-001/)).toBeVisible()
    expect(materializeDbrPurchasePlan).toHaveBeenNthCalledWith(2, {
      active: true, thresholdDays: 60, dryRun: false,
    })
    await waitFor(() => expect(previewDbrPurchasePlan).toHaveBeenCalledTimes(2))
  })

  it('closes the dialog and reports a dry-run failure on the page', async () => {
    const user = userEvent.setup()
    vi.mocked(materializeDbrPurchasePlan).mockReset().mockRejectedValue(new Error('Предпросмотр не выполнен'))
    renderPage()
    await calculate(user)

    await user.click(screen.getByRole('button', { name: 'Сформировать заказы…' }))
    expect(await screen.findByText('Предпросмотр не выполнен')).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('keeps a failed confirmation in the dialog and does not refresh the preview', async () => {
    const user = userEvent.setup()
    vi.mocked(materializeDbrPurchasePlan)
      .mockReset()
      .mockResolvedValueOnce(dryRun)
      .mockRejectedValueOnce(new Error('1С отклонила документ'))
    renderPage()
    await calculate(user)
    await user.click(screen.getByRole('button', { name: 'Сформировать заказы…' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Провести в 1С' }))

    expect(await within(dialog).findByText('1С отклонила документ')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Провести в 1С' })).toBeEnabled()
    expect(previewDbrPurchasePlan).toHaveBeenCalledTimes(1)
  })

  it('locks preview materialization controls while the dry-run mutation is pending', async () => {
    const user = userEvent.setup()
    const pending = deferred<DbrPurchaseLaunchResult>()
    vi.mocked(materializeDbrPurchasePlan).mockReset().mockReturnValue(pending.promise)
    renderPage()
    await calculate(user)

    const launch = screen.getByRole('button', { name: 'Сформировать заказы…' })
    fireEvent.click(launch)
    fireEvent.click(launch)
    expect(materializeDbrPurchasePlan).toHaveBeenCalledTimes(1)
    expect(launch).toBeDisabled()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Отмена' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Отправка…' })).toBeDisabled()

    pending.resolve(dryRun)
    expect(await within(dialog).findByText('Документ № PREVIEW-1')).toBeVisible()
  })

  it('does not let a failed program bootstrap prevent active-plan calculation', async () => {
    const user = userEvent.setup()
    vi.mocked(listDbrPrograms).mockRejectedValue(new Error('Программы недоступны'))
    renderPage()
    await calculate(user)

    expect(screen.getByText('ITEM-1')).toBeVisible()
    expect(previewDbrPurchasePlan).toHaveBeenCalledWith({ active: true, thresholdDays: 60 })
  })

  it.todo('keeps the newest calculation when an older preview request resolves last')
  it.todo('locks confirmation synchronously against duplicate write requests')
  it.todo('traps focus, restores the opener, and closes the confirmation dialog with Escape')
})
