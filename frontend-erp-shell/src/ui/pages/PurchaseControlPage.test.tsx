import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PurchaseRow } from '../../domain/purchaseControl'
import {
  exportPurchasesTo1C,
  getPurchaseFilters,
  getPurchaseOrderCard,
  listPurchaseJournal,
  syncSupplierOrdersFrom1C,
} from '../../services/purchaseControl'
import { PurchaseControlPage } from './PurchaseControlPage'

vi.mock('../../services/purchaseControl', () => ({
  exportPurchasesTo1C: vi.fn(),
  getPurchaseFilters: vi.fn(),
  getPurchaseOrderCard: vi.fn(),
  listPurchaseJournal: vi.fn(),
  syncSupplierOrdersFrom1C: vi.fn(),
}))

const purchaseRow: PurchaseRow = {
  row_key: 'purchase:41',
  line_id: null,
  purchase_id: 41,
  source_purchase_ids: [41, 42],
  order_id: null,
  order_number: '',
  order_date: '2026-07-20',
  order_ref1c: null,
  order_state_name: null,
  source: 'mrp',
  supplier_id: 7,
  supplier_name: 'Промснаб',
  item_id: 9,
  item_code: 'BEARING-01',
  item_article: 'ПД-01',
  item_name: 'Подшипник ведущего вала',
  unit: 'шт',
  quantity: 12,
  received_qty: 0,
  remaining_qty: 12,
  delivery_date: null,
  need_date: '2026-07-25',
  overdue_days: 0,
  line_status: 'to_order',
  supply_phase: 'no_goods',
  counts_in_mrp: true,
  price: 100,
  amount: 1200,
  run_id: 17,
}

const orderedRow: PurchaseRow = {
  ...purchaseRow,
  row_key: 'order-line:52',
  line_id: 52,
  purchase_id: null,
  source_purchase_ids: [],
  order_id: 8,
  order_number: 'ЗП-000008',
  order_ref1c: 'order-ref',
  order_state_name: 'К поступлению',
  line_status: 'expected',
  supply_phase: 'in_transit',
}

function renderPage(url = '/purchase-control') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <PurchaseControlPage />
    </MemoryRouter>,
  )
}

describe('PurchaseControlPage Doctype migration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listPurchaseJournal).mockResolvedValue({
      rows: [purchaseRow, orderedRow],
      total: 2,
      limit: 100,
      offset: 0,
      run_id: 17,
      summary: {
        total_rows: 2,
        by_status: { to_order: 1, expected: 1 },
        by_phase: { no_goods: 1, in_transit: 1 },
        to_order: 1,
        overdue: 0,
        expected_7d: 2,
        in_transit_amount: 1200,
      },
    })
    vi.mocked(getPurchaseFilters).mockResolvedValue({
      suppliers: [{ supplier_id: 7, supplier_name: 'Промснаб' }],
      states: ['К поступлению'],
    })
    vi.mocked(getPurchaseOrderCard).mockResolvedValue({
      order: {
        order_id: 8,
        order_number: 'ЗП-000008',
        order_date: '2026-07-20',
        order_ref1c: 'order-ref',
        order_state_name: 'К поступлению',
        supply_phase: 'in_transit',
        counts_in_mrp: true,
        deletion_mark: false,
        is_posted: true,
        document_amount: 1200,
        active: true,
        source: 'mrp',
        supplier_id: 7,
        supplier_name: 'Промснаб',
      },
      lines: [orderedRow],
    })
    vi.mocked(exportPurchasesTo1C).mockResolvedValue({ orders_created: 1, orders_existing: 0 })
    vi.mocked(syncSupplierOrdersFrom1C).mockResolvedValue({ orders_created: 0, orders_updated: 1 })
  })

  it('preserves dense journal rows, summary controls and explicit search', async () => {
    renderPage('/purchase-control?order_id=8&search=вал')

    expect((await screen.findAllByText('Подшипник ведущего вала')).length).toBeGreaterThan(0)
    expect(screen.getByText('MRP #41 +1')).toBeInTheDocument()
    expect(screen.getByText('ЗП-000008')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Нет товара: 1' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Промснаб' })).toBeInTheDocument()

    expect(vi.mocked(listPurchaseJournal).mock.calls[0]?.[0].get('order_id')).toBe('8')
    expect(vi.mocked(listPurchaseJournal).mock.calls[0]?.[0].get('search')).toBe('вал')

    fireEvent.change(screen.getByLabelText('Поиск'), { target: { value: 'подшипник' } })
    expect(listPurchaseJournal).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(screen.getByLabelText('Поиск'), { key: 'Enter' })
    await waitFor(() => expect(listPurchaseJournal).toHaveBeenCalledTimes(2))
    expect(vi.mocked(listPurchaseJournal).mock.calls[1]?.[0].get('search')).toBe('подшипник')
  })

  it('keeps guarded MRP selection and export to 1C flow', async () => {
    renderPage()
    await screen.findByText('MRP #41 +1')

    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(1)
    fireEvent.click(checkboxes[0])
    fireEvent.click(screen.getByRole('button', { name: 'Заказать в 1С (1)' }))

    await waitFor(() => expect(exportPurchasesTo1C).toHaveBeenCalledWith(17, [41, 42]))
    expect(await screen.findByText('Заказы поставщику: создано 1, уже было 0')).toBeInTheDocument()
    await waitFor(() => expect(syncSupplierOrdersFrom1C).toHaveBeenCalled())
  })

  it('delegates sorting and instant filters to the runtime data source', async () => {
    renderPage()
    await screen.findByText('MRP #41 +1')

    fireEvent.click(screen.getByRole('button', { name: /^Поставка/ }))
    await waitFor(() => expect(listPurchaseJournal).toHaveBeenCalledTimes(2))
    expect(vi.mocked(listPurchaseJournal).mock.calls[1]?.[0].get('sort_by')).toBe('delivery_date')
    expect(vi.mocked(listPurchaseJournal).mock.calls[1]?.[0].get('sort_dir')).toBe('asc')

    fireEvent.change(screen.getByLabelText('Поставщик'), { target: { value: '7' } })
    await waitFor(() => expect(listPurchaseJournal).toHaveBeenCalledTimes(3))
    expect(vi.mocked(listPurchaseJournal).mock.calls[2]?.[0].get('supplier_id')).toBe('7')
  })
})
