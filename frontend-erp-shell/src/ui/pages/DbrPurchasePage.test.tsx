import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DbrPurchaseCockpit } from '../../domain/dbr'
import { ApiError } from '../../lib/api'
import { getDbrPurchaseCockpit } from '../../services/dbr'
import { DbrPurchasePage } from './DbrPurchasePage'

vi.mock('../../services/dbr', () => ({
  getDbrPurchaseCockpit: vi.fn(),
  dbrSnapshotUnavailableMessage: (error: unknown) => {
    const candidate = error as { status?: number; detail?: { code?: string; reason?: string }; message?: string }
    return candidate?.status === 503 && candidate.detail
      ? `${candidate.detail.code ?? 'snapshot_unavailable'}: ${candidate.detail.reason ?? candidate.message ?? ''}`
      : null
  },
}))

const cockpit: DbrPurchaseCockpit = {
  meta: {
    snapshot_id: 71,
    ledger_generation: 42,
    cutoff: '2026-07-23T10:00:00Z',
    runs: [{ run_id: 17, freeze_version: 9 }, { run_id: 18, freeze_version: 4 }],
    truth_status: 'accepted',
    read_only: true,
  },
  rows: [
    { item_id: 1, item_code: 'ITEM-1', item_name: 'Подшипник', supplier_ref1c: 'SUPPLIER-1', warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [101], obligations: [{ reservation_id: 101, priority_period_from: '2026-08-20', priority_period_to: '2026-08-20', outstanding_qty: 10, uncovered_qty: 7, coverage: [] }], outstanding_obligation_qty: 10, uncovered_qty: 7, to_order_qty: 7, stock_qty: 2, exact_future_supply_qty: 1, need_date: '2026-08-20' },
    { item_id: 2, item_code: 'ITEM-2', item_name: 'Редуктор', supplier_ref1c: null, warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [102], obligations: [{ reservation_id: 102, priority_period_from: '2026-10-01', priority_period_to: '2026-10-01', outstanding_qty: 5, uncovered_qty: 5, coverage: [] }], outstanding_obligation_qty: 5, uncovered_qty: 5, to_order_qty: 5, stock_qty: 0, exact_future_supply_qty: 0, need_date: '2026-10-01' },
    { item_id: 3, item_code: 'ITEM-3', item_name: 'Крепёж', supplier_ref1c: 'SUPPLIER-2', warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [103], obligations: [{ reservation_id: 103, outstanding_qty: 1, uncovered_qty: 0, coverage: [] }], outstanding_obligation_qty: 1, uncovered_qty: 0, to_order_qty: 0, stock_qty: 4, exact_future_supply_qty: 0, need_date: null },
  ],
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

function renderPage() {
  return render(<MemoryRouter><DbrPurchasePage /></MemoryRouter>)
}

describe('DbrPurchasePage saved-snapshot characterization', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(getDbrPurchaseCockpit).mockResolvedValue(cockpit)
  })

  it('loads exactly one immutable cockpit GET on mount', async () => {
    renderPage()
    expect(await screen.findByText('ITEM-1')).toBeVisible()
    expect(getDbrPurchaseCockpit).toHaveBeenCalledTimes(1)
  })

  it('does not render legacy source, threshold, calculation, or materialize controls', async () => {
    renderPage()
    await screen.findByText('ITEM-1')
    expect(screen.queryByRole('combobox', { name: 'Источник' })).not.toBeInTheDocument()
    expect(screen.queryByRole('spinbutton', { name: 'Горизонт заказа, дней' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Рассчитать' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Сформировать заказы…' })).not.toBeInTheDocument()
  })

  it('renders explicit ledger snapshot and frozen MRP lineage', async () => {
    renderPage()
    expect(await screen.findByTestId('purchase-snapshot-lineage')).toHaveTextContent('Снимок #71')
    expect(screen.getByTestId('purchase-snapshot-lineage')).toHaveTextContent('Ledger-поколение #42')
    expect(screen.getByTestId('purchase-snapshot-lineage')).toHaveTextContent('run #17')
    expect(screen.getByTestId('purchase-snapshot-lineage')).toHaveTextContent('freeze 9')
    expect(screen.getByTestId('purchase-snapshot-lineage')).toHaveTextContent('run #18')
  })

  it('derives read-only KPIs from captured rows', async () => {
    renderPage()
    await screen.findByText('ITEM-1')
    expect(screen.getByText('Позиций в снимке').parentElement).toHaveTextContent('3')
    expect(screen.getByText('К заказу').parentElement).toHaveTextContent('2')
    expect(screen.queryByText('Срочные')).not.toBeInTheDocument()
  })

  it('keeps positive-obligation filtering local to the saved rows', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('ITEM-1')
    expect(screen.queryByText('ITEM-3')).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Только к заказу' }))
    expect(screen.getByText('ITEM-3')).toBeVisible()
    expect(getDbrPurchaseCockpit).toHaveBeenCalledTimes(1)
  })

  it('sorts snapshot rows locally by article', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('ITEM-1')
    await user.click(screen.getByRole('checkbox', { name: 'Только к заказу' }))
    await user.click(screen.getByRole('columnheader', { name: 'Позиция' }))
    const body = document.querySelector('.dbrPurchaseTable tbody')
    expect(within(body as HTMLElement).getAllByRole('row').map((row) => row.textContent)).toEqual([
      expect.stringContaining('ITEM-1'), expect.stringContaining('ITEM-2'), expect.stringContaining('ITEM-3'),
    ])
    expect(getDbrPurchaseCockpit).toHaveBeenCalledTimes(1)
  })

  it('sorts the captured rows by required quantity from the keyboard', async () => {
    const user = userEvent.setup()
    renderPage()
    const table = await screen.findByRole('table', { name: 'Сохранённые Ledger-обязательства закупки' })
    const quantity = within(table).getByRole('columnheader', { name: 'Непокрыто / заказать' })
    quantity.focus()
    await user.keyboard('{Enter}')
    expect(quantity).toHaveAttribute('aria-sort', 'descending')
    expect(within(table).getAllByRole('row')[1]).toHaveTextContent('ITEM-1')
  })

  it('renders Ledger-native quantities without legacy demand or available columns', async () => {
    renderPage()
    expect(await screen.findByText('ITEM-1')).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Обязательство' })).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Ledger-запас' })).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Точное поступление' })).toBeVisible()
    expect(screen.queryByRole('columnheader', { name: 'Потребность' })).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Доступно' })).not.toBeInTheDocument()
    expect(getDbrPurchaseCockpit).toHaveBeenCalledTimes(1)
  })

  it('shows the saved snapshot empty state', async () => {
    vi.mocked(getDbrPurchaseCockpit).mockResolvedValue({ meta: { snapshot_id: 3, read_only: true, runs: [] }, rows: [] })
    renderPage()
    expect(await screen.findByText('Нет позиций к заказу.')).toBeVisible()
  })

  it('renders a structured 503 as snapshot-unavailable instead of fake zeroes', async () => {
    vi.mocked(getDbrPurchaseCockpit).mockRejectedValue(new ApiError('not ready', 503, { code: 'ledger_not_accepted', reason: 'generation 42 is building' }))
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('ledger_not_accepted: generation 42 is building')
    expect(screen.getByText('Сохранённый снимок закупки недоступен.')).toBeVisible()
    expect(screen.queryByText('Позиций в снимке')).not.toBeInTheDocument()
  })

  it('announces loading and preserves the read-only refresh action focus', async () => {
    const pending = deferred<DbrPurchaseCockpit>()
    vi.mocked(getDbrPurchaseCockpit).mockReturnValue(pending.promise)
    renderPage()
    expect(screen.getByRole('status')).toHaveTextContent('Загрузка сохранённого снимка закупки')
    const refresh = screen.getByRole('button', { name: 'Обновить снимок' })
    pending.resolve(cockpit)
    await screen.findByText('ITEM-1')
    await userEvent.setup().click(refresh)
    expect(refresh).toHaveFocus()
  })

  it('refreshes the saved envelope only after an explicit read-only request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('ITEM-1')
    await user.click(screen.getByRole('button', { name: 'Обновить снимок' }))
    await waitFor(() => expect(getDbrPurchaseCockpit).toHaveBeenCalledTimes(2))
  })

  it('keeps local filters while an explicit snapshot refresh is pending', async () => {
    const pending = deferred<DbrPurchaseCockpit>()
    vi.mocked(getDbrPurchaseCockpit).mockResolvedValueOnce(cockpit).mockReturnValueOnce(pending.promise)
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('ITEM-1')
    await user.click(screen.getByRole('checkbox', { name: 'Только к заказу' }))
    await user.click(screen.getByRole('button', { name: 'Обновить снимок' }))
    expect(screen.getByText('ITEM-3')).toBeVisible()
    pending.resolve({ ...cockpit, rows: [{ ...cockpit.rows[0], item_id: 99, item_code: 'LATEST-ITEM' }] })
    expect(await screen.findByText('LATEST-ITEM')).toBeVisible()
    expect(screen.queryByText('ITEM-3')).not.toBeInTheDocument()
  })
})
