import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PurchaseRow } from '../../domain/purchaseControl'
import {
  materializePurchaseControlRows,
  getPurchaseFilters,
  getPurchaseOrderCard,
  listPurchaseJournal,
  syncSupplierOrdersFrom1C,
} from '../../services/purchaseControl'
import { PurchaseControlPage } from './PurchaseControlPage'

vi.mock('../../services/purchaseControl', () => ({
  materializePurchaseControlRows: vi.fn(),
  getPurchaseFilters: vi.fn(),
  getPurchaseOrderCard: vi.fn(),
  listPurchaseJournal: vi.fn(),
  syncSupplierOrdersFrom1C: vi.fn(),
}))

const purchaseRow: PurchaseRow = {
  row_key: 'buy:9:default',
  line_id: null,
  purchase_id: null,
  source_purchase_ids: [],
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
  row_generator: 'mrp_reservation',
  to_order_qty: 12,
  required_qty: 12,
  realized_qty: 0,
  open_order_covered_qty: 0,
  delivery_date: null,
  need_date: '2026-07-25',
  overdue_days: 0,
  line_status: 'to_order',
  supply_phase: 'no_goods',
  counts_in_mrp: true,
  price: 100,
  amount: 1200,
  run_id: 17,
  run_ids: [17, 18],
  requirement_ids: [101, 102],
  reservation_ids: [201, 202],
  planning_stock_pool: 'default',
  open_order_covered_pct: 0,
  to_order_pct: 100,
  fact_status: 'available',
  fact_source: 'mrp',
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
  row_generator: 'ledger_future_supply',
  received_qty: null,
  line_status: 'unavailable',
  supply_phase: 'in_transit',
  fact_status: 'unavailable',
  fact_source: 'ledger_future_supply',
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
      rows: [purchaseRow],
      total: 1,
      limit: 100,
      offset: 0,
      run_id: 17,
      run_ids: [17],
      truth_status: 'accepted',
      ledger_generation_id: 23,
      meta: {
        snapshot_id: 51,
        ledger_generation: 23,
        ledger_generation_id: 23,
        cutoff: '2026-07-23T12:00:00+00:00',
        truth_status: 'accepted',
        truth_reason: null,
        fact_source: 'ledger',
        received_qty_status: 'available',
        read_only: true,
      },
      summary: {
        total_rows: 1,
        by_status: { to_order: 1 },
        by_phase: { no_goods: 1 },
        to_order: 1,
        overdue: 0,
        expected_7d: 2,
        in_transit_amount: 1200,
        fact_status: 'available',
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
      meta: {
        snapshot_id: 51,
        ledger_generation: 23,
        cutoff: '2026-07-23T12:00:00+00:00',
        truth_status: 'accepted',
        fact_source: 'ledger',
        received_qty_status: 'unavailable',
        read_only: true,
      },
    })
    vi.mocked(syncSupplierOrdersFrom1C).mockResolvedValue({ orders_created: 0, orders_updated: 1 })
    vi.mocked(materializePurchaseControlRows).mockResolvedValue({
      snapshot_id: 51,
      rows_total: 1,
      dry_run: false,
      status: 'completed',
    })
  })

  it('preserves dense journal rows, summary controls and explicit search', async () => {
    renderPage('/purchase-control?order_id=8&search=вал')

    expect((await screen.findAllByText('Подшипник ведущего вала')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('Под заказ (MRP)').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Нет товара: 1' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Промснаб' })).toBeInTheDocument()
    expect(screen.getByText(/оформлено 0% · к заказу 100%/)).toBeInTheDocument()

    expect(vi.mocked(listPurchaseJournal).mock.calls[0]?.[0].get('order_id')).toBe('8')
    expect(vi.mocked(listPurchaseJournal).mock.calls[0]?.[0].get('search')).toBe('вал')

    fireEvent.change(screen.getByLabelText('Поиск'), { target: { value: 'подшипник' } })
    expect(listPurchaseJournal).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(screen.getByLabelText('Поиск'), { key: 'Enter' })
    await waitFor(() => expect(listPurchaseJournal).toHaveBeenCalledTimes(2))
    expect(vi.mocked(listPurchaseJournal).mock.calls[1]?.[0].get('search')).toBe('подшипник')
  })

  it('keeps guarded MRP selection and materialization flow', async () => {
    renderPage()
    await screen.findAllByText('Под заказ (MRP)')

    fireEvent.click(screen.getByRole('checkbox', { name: 'Выбрать строку buy:9:default' }))
    fireEvent.click(screen.getByRole('button', { name: 'Сформировать заказы (1)' }))

    await waitFor(() => expect(materializePurchaseControlRows).toHaveBeenCalledWith({ snapshot_id: 51, row_keys: ['buy:9:default'], dry_run: false }))
    expect(await screen.findByText('Сформировано заказов по 1 строкам снапшота')).toBeInTheDocument()
    await waitFor(() => expect(syncSupplierOrdersFrom1C).toHaveBeenCalled())
  })

  it('delegates sorting and instant filters to the runtime data source', async () => {
    renderPage()
    await screen.findAllByText('Под заказ (MRP)')

    fireEvent.click(screen.getByRole('button', { name: /^Поставка/ }))
    await waitFor(() => expect(listPurchaseJournal).toHaveBeenCalledTimes(2))
    expect(vi.mocked(listPurchaseJournal).mock.calls[1]?.[0].get('sort_by')).toBe('delivery_date')
    expect(vi.mocked(listPurchaseJournal).mock.calls[1]?.[0].get('sort_dir')).toBe('asc')

    fireEvent.change(screen.getByLabelText('Поставщик'), { target: { value: '7' } })
    await waitFor(() => expect(listPurchaseJournal).toHaveBeenCalledTimes(3))
    expect(vi.mocked(listPurchaseJournal).mock.calls[2]?.[0].get('supplier_id')).toBe('7')
  })
})
