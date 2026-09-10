import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'

import { ProductionControlPage } from './ProductionControlPage'
import { ApiError } from '../../lib/api'
import type { MaterialsResponse, OrderRow } from '../../domain/productionControl'
import type { ProductionResource } from '../../domain/resources'

// --- Service layer mocks: no real network is allowed. Every named export the
// page imports is replaced with a vi.fn() so we can both feed fake data and
// assert action calls. Domain types + child components stay REAL so we test the
// component's real rendering/wiring.
vi.mock('../../services/productionControl', () => ({
  listProductionOrders: vi.fn(),
  listDrumSchedule: vi.fn(),
  moveDrumSlot: vi.fn(),
  listProductionEmployees: vi.fn(),
  listProductionOperations: vi.fn(),
  getProductionControlSettings: vi.fn(),
  listRootProductOptions: vi.fn(),
  materializeMakeWorkItems: vi.fn(),
  openPaintWeldChains: vi.fn(),
  closePaintWeldChain: vi.fn(),
  getStandalonePieceworkOptions: vi.fn(),
  createStandalonePiecework: vi.fn(),
  saveProductionControlSettings: vi.fn(),
  getOrderMaterials: vi.fn(),
  getWorkItemMaterials: vi.fn(),
  updateOrderStatus: vi.fn(),
  postMaterialIssues: vi.fn(),
  fetchRouteSheetsPrintHtml: vi.fn(),
  exportMaterialIssuesTo1C: vi.fn(),
  markMaterialIssueAssembled: vi.fn(),
  syncExecutionFrom1C: vi.fn(),
  produceOrderLine: vi.fn(),
  getItem: vi.fn(),
  updateItem: vi.fn(),
  updateOrderQuantity: vi.fn(),
  deleteProductionOrder: vi.fn(),
}))

vi.mock('../../services/resources', () => ({
  listResources: vi.fn(),
}))

vi.mock('../../services/itemLedger', () => ({
  getItemLedgerPosition: vi.fn(),
  getItemLedgerReservations: vi.fn(),
  getItemLedgerFutureSupply: vi.fn(),
}))

import {
  listProductionOrders,
  listDrumSchedule,
  moveDrumSlot,
  listProductionEmployees,
  listProductionOperations,
  getOrderMaterials,
  getWorkItemMaterials,
  updateOrderStatus,
  postMaterialIssues,
  exportMaterialIssuesTo1C,
  syncExecutionFrom1C,
  deleteProductionOrder,
  fetchRouteSheetsPrintHtml,
  produceOrderLine,
  listRootProductOptions,
  materializeMakeWorkItems,
  openPaintWeldChains,
  closePaintWeldChain,
  getStandalonePieceworkOptions,
  createStandalonePiecework,
  updateOrderQuantity,
} from '../../services/productionControl'
import { listResources } from '../../services/resources'
import {
  getItemLedgerFutureSupply,
  getItemLedgerPosition,
  getItemLedgerReservations,
} from '../../services/itemLedger'

// --- Fake data shaped to the domain types ---------------------------------
// row 101: local order (no 1C ref), coverage 'assembled' + issue 'posted'
//   => produceable AND deletable.
// row 102: opened in 1C (has order_ref1c) => not deletable, shortage.
function fakeRows(): OrderRow[] {
  return [
    {
      product_id: 101,
      order_id: 101,
      item_id: 201,
      item_code: 'ART-1',
      item_article: 'ART-1',
      order_number: 'ORD-1',
      order_prodplan_number: 'PP-1',
      order_source: 'mrp',
      source: 'mrp',
      order_ref1c: null,
      item_name: 'Кронштейн',
      unit: 'шт',
      quantity: 10,
      produced_qty: 0,
      remaining_qty: 10,
      status: 'ready',
      coverage_status: 'assembled',
      coverage_label: 'Собрано',
      issue_status: 'posted',
      issue_count: 0,
      workshop_name: 'Цех 1',
      launch_source: 'mrp_remaining',
      available_actions: ['close_1c'],
      comment: '',
    },
    {
      product_id: 102,
      order_id: 102,
      item_id: 202,
      item_code: 'ART-2',
      item_article: 'ART-2',
      order_number: 'ORD-2',
      order_source: '1c',
      source: '1c',
      order_ref1c: 'REF-2',
      order_one_c_number: '1C-2',
      item_name: 'Вал',
      unit: 'шт',
      quantity: 5,
      produced_qty: 0,
      remaining_qty: 5,
      status: 'shortage',
      coverage_status: 'shortage',
      coverage_label: 'Дефицит',
      workshop_name: 'Цех 2',
      issue_status: 'missing',
      issue_count: 0,
      launch_source: 'mrp_remaining',
      available_actions: [],
      comment: '',
    },
  ]
}

function fakeMaterials(): MaterialsResponse {
  return {
    ledger_generation_id: 77,
    truth_status: 'accepted',
    cutoff: '2026-07-31T12:00:00+00:00',
    product_id: 1,
    order_number: 'ORD-1',
    item_name: 'Кронштейн',
    item_article: 'BRACKET-1',
    qty: 10,
    spec_id: null,
    coverage: 'assembled',
    coverage_status: 'assembled',
    coverage_label: 'Собрано',
    coverage_basis: 'direct_bom',
    components: [
      {
        component_item_id: 301,
        item_name: 'Болт М8',
        item_article: 'BOLT-8',
        qty_per_unit: 4,
        available_qty: 40,
        required_qty: 40,
        missing_qty: 0,
        unit: 'шт',
        coverage_status: 'assembled',
        coverage_label: 'Собрано',
      },
    ],
  }
}

const fakeResources: ProductionResource[] = [
  { resource_id: 1, resource_name: 'Цех 1' },
  { resource_id: 2, resource_name: 'Цех 2' },
]

const fakeRootOptions = [
  { item_id: 301, item_name: 'Корень 1', item_article: 'R-001', item_code: 'R1' },
  { item_id: 302, item_name: 'Корень 2', item_article: 'R-002', item_code: 'R2' },
]

const fakeTruthMeta = {
  ledger_generation: 77,
  cutoff: '2026-07-31T00:00:00Z',
  truth_status: 'finalized',
  truth_reason: null,
}

function renderPage(initialEntries: string[] = ['/production-control']) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <ProductionControlPage />
      <LocationProbe />
    </MemoryRouter>,
  )
}

function LocationProbe() {
  const location = useLocation()
  return <span hidden data-testid="location-search">{location.search}</span>
}

// Grab the orders table (not the column-filter table, which shares a class).
function ordersTable(container: HTMLElement): HTMLElement {
  const el = container.querySelector('table.productionOrdersTable:not(.columnFilterTable)')
  if (!el) throw new Error('orders table not found')
  return el as HTMLElement
}

function filterTable(container: HTMLElement): HTMLElement {
  const el = container.querySelector('table.columnFilterTable')
  if (!el) throw new Error('filter table not found')
  return el as HTMLElement
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

// Scope to the orders table: item names (e.g. 'Кронштейн') also appear in the
// detail pane, so an unscoped getByText would match multiple elements.
function rowFor(name: string): HTMLElement {
  const cell = within(ordersTable(document.body)).getByText(name)
  const tr = cell.closest('tr')
  if (!tr) throw new Error(`row for ${name} not found`)
  return tr
}

beforeEach(() => {
  vi.clearAllMocks()
  // Deterministic print/confirm behaviour for jsdom.
  vi.spyOn(window, 'open').mockReturnValue(null)
  vi.spyOn(window, 'confirm').mockReturnValue(true)

  vi.mocked(listProductionOrders).mockResolvedValue({
    rows: fakeRows(),
    total: 2,
    limit: 100,
    offset: 0,
    latest_run_id: 77,
    truth_meta: fakeTruthMeta,
  })
  vi.mocked(listDrumSchedule).mockResolvedValue({
    schedule_from: '2026-09-03',
    schedule_to: '2026-10-02',
    days: ['2026-09-03', '2026-09-04'],
    resources: [{ resource_id: 1, resource_name: 'Цех 1' }],
    slots: [{
      slot_id: 501,
      queue_line_id: 901,
      run_id: 77,
      plan_id: 1,
      plan_line_id: 11,
      period_from: '2026-09-01',
      period_to: '2026-09-30',
      item_id: 201,
      item_code: 'ART-1',
      item_name: 'Кронштейн',
      resource_id: 1,
      slot_date: '2026-09-03',
      slot_qty: 4,
      slot_ordinal: 0,
      readiness_phase: 'now',
      readiness_date: '2026-09-03',
      readiness_curve: [
        { horizon: 'now', cumulative_qty: '4', available_date: '2026-09-03', actions: [] },
        { horizon: 'transfer', cumulative_qty: '4', available_date: '2026-09-03', actions: [] },
        { horizon: 'kitting', cumulative_qty: '4', available_date: '2026-09-03', actions: [] },
        { horizon: 'committed', cumulative_qty: '4', available_date: '2026-09-03', actions: [] },
        { horizon: 'launch', cumulative_qty: '4', available_date: '2026-09-03', actions: [] },
      ],
      action_manifest: [],
      unavailable_reasons: [],
      blocking_manifest: [],
      manual_override: false,
      original_priority: ['2026-08-01', 11],
    }],
    gaps: [],
    excluded: [],
    total_open_qty: 10,
    total_slot_qty: 4,
    total_gap_qty: 6,
    total_slots: 1,
    total_gaps: 0,
    total_excluded: 0,
    total_excluded_open_qty: 0,
    limit: 10000,
    offset: 0,
    truth_meta: fakeTruthMeta,
  })
  vi.mocked(moveDrumSlot).mockResolvedValue({
    ok: true,
    moved: true,
    slot_id: 501,
    from_date: '2026-09-03',
    to_date: '2026-09-04',
    resource_id: 1,
  })
  vi.mocked(getOrderMaterials).mockResolvedValue(fakeMaterials())
  vi.mocked(getWorkItemMaterials).mockResolvedValue({ ...fakeMaterials(), product_id: null, work_item_id: 701 })
  vi.mocked(getItemLedgerPosition).mockResolvedValue({
    item_id: 201,
    item_code: 'ART-1',
    item_name: 'Кронштейн',
    pool_key: '201::default',
    on_hand: 12,
    on_hand_by_warehouse: [{ warehouse_ref1c: 'WH-1', warehouse_name: 'Основной склад', qty: 12, qty_negative: false }],
    incoming_supplier: 7,
    incoming_wip: 3,
    incoming: 10,
    reserved_soft: 9,
    available: 3,
    projected: 13,
    uncovered: 0,
    flags: { on_hand_negative: false, has_uncovered: false, reconcile_pending: false },
    truth_meta: { ...fakeTruthMeta, truth_status: 'accepted' },
  })
  vi.mocked(getItemLedgerReservations).mockResolvedValue({
    rows: [{
      reservation_id: 501,
      run_id: 77,
      plan_id: 1,
      plan_name: 'План августа',
      requirement_id: 9001,
      realization_mode: 'make',
      priority: { period_from: '2026-08-01', period_to: '2026-08-31' },
      reserved_qty: 9,
      covered_from_stock_at_freeze_qty: 0,
      replenishment_required_qty: 9,
      replenishment_received_qty: 2,
      replenishment_remaining_qty: 7,
      lifecycle_status: 'active',
    }],
    truth_meta: { ...fakeTruthMeta, truth_status: 'accepted' },
  })
  vi.mocked(getItemLedgerFutureSupply).mockResolvedValue({
    rows: [{
      id: 601,
      supply_kind: 'supplier_order',
      source_ref: 'b0d16efe-6553-11f1-9270-9ee51454587f',
      source_number: 'ЗП-000042',
      source_line_ref: '1',
      ordered_qty: 7,
      received_qty: 0,
      open_qty: 7,
      eta_date: '2026-08-15',
      destination_warehouse_ref1c: 'WH-1',
      destination_warehouse_name: 'Основной склад',
      source_state_key: 'ordered',
      source_state_name: 'Заказан',
      evidence_status: 'exact',
    }],
    truth_meta: { ...fakeTruthMeta, truth_status: 'accepted' },
  })
  vi.mocked(listResources).mockResolvedValue(fakeResources)
  vi.mocked(listRootProductOptions).mockResolvedValue({
    rows: fakeRootOptions,
    total: fakeRootOptions.length,
  })
  vi.mocked(listProductionEmployees).mockResolvedValue({
    rows: [{ employee_id: 1, employee_ref1c: 'E1', employee_type: 'employee', employee_name: 'Иванов' }],
    total: 1,
  })
  vi.mocked(listProductionOperations).mockResolvedValue({ rows: [], total: 0 })
  vi.mocked(materializeMakeWorkItems).mockResolvedValue({ status: 'ok', created: [], reused: [] })
  vi.mocked(openPaintWeldChains).mockImplementation(async (productIds) => ({
    status: 'ok', product_ids: productIds, entries: [], errors: [],
  }))
  vi.mocked(closePaintWeldChain).mockResolvedValue({ status: 'ok' })
  vi.mocked(updateOrderStatus).mockResolvedValue({} as never)
  vi.mocked(deleteProductionOrder).mockResolvedValue({} as never)
  vi.mocked(produceOrderLine).mockResolvedValue({ qty: 10 } as never)
  vi.mocked(fetchRouteSheetsPrintHtml).mockResolvedValue('<html></html>')
  vi.mocked(postMaterialIssues).mockResolvedValue({
    status: 'ok',
    created: [{ issue_id: 1, document_number: 'MI-1', product_id: 101 }],
    reused: [],
    selection_required: [],
    already_on_destination: [],
    errors: [],
  })
  vi.mocked(exportMaterialIssuesTo1C).mockResolvedValue({
    status: 'ok',
    issues_created: 1,
    issues_already_linked: 0,
    issues_error: 0,
    parent_orders_export: { orders_created: 1, orders_already_linked: 0, orders_error: 0, status: 'ok' },
    entries: [],
    skipped_rows: [],
  })
  vi.mocked(syncExecutionFrom1C).mockResolvedValue({
    orders: { orders_updated: 1, errors: [] },
    transfers: { candidates: 2, advanced: 0, errors: [] },
  } as never)
})

describe('ProductionControlPage — characterization', () => {
  it('shows accepted Ledger balances, active reservations and live orders for the selected item', async () => {
    renderPage()

    expect(await screen.findByText('Ledger по номенклатуре')).toBeInTheDocument()
    expect(await screen.findByText('Основной склад')).toBeInTheDocument()
    expect(screen.getByText(/План августа · треб. #9001/)).toBeInTheDocument()
    expect(screen.getByText(/Заказ поставщику ЗП-000042/)).toBeInTheDocument()
    expect(getItemLedgerPosition).toHaveBeenCalledWith(201, expect.anything())
    expect(getItemLedgerReservations).toHaveBeenCalledWith(201, { status: 'active' }, expect.anything())
    expect(getItemLedgerFutureSupply).toHaveBeenCalledWith(201, expect.anything())
  })

  it('renders the page shell: heading, command bar, and table columns', async () => {
    const { container } = renderPage()
    await screen.findByText('Вал')

    // Breadcrumb + document heading
    expect(screen.getByRole('heading', { name: 'Журнал заказов на производство' })).toBeInTheDocument()
    expect(screen.getByText('Производство / Журнал заказов на производство')).toBeInTheDocument()
    expect(screen.getByText(/Истина недоступна: finalized · Ledger 77/)).toBeVisible()

    // Command bar buttons
    for (const label of [
      'Запустить в 1С',
      'Произвести',
      'Синхронизировать',
      'Печать маршрутных',
      'Удалить',
      'Обновить',
      'Выбрать все',
      'Снять выбор',
      'Корневое изделие',
      'Настройки',
    ]) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    }

    // Orders table column headers (sortable ones carry a sort glyph, so match
    // against the combined header text rather than exact strings).
    const table = ordersTable(container)
    const headerText = within(table)
      .getAllByRole('columnheader')
      .map((h) => h.textContent ?? '')
      .join('|')
    for (const col of ['Заказ', 'Деталь', 'Выпуск заказа', 'План', 'Участок', 'Статус', 'Обеспечение']) {
      expect(headerText).toContain(col)
    }
  })

  it('shows saved order output facts in the journal and selected card without deriving the remainder', async () => {
    const rows = fakeRows()
    rows[0] = { ...rows[0], quantity: 10, produced_qty: 4, remaining_qty: 6 }
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows,
      total: 2,
      limit: 100,
      offset: 0,
      latest_run_id: 77,
      truth_meta: { ...fakeTruthMeta, truth_status: 'accepted' },
    })

    renderPage()
    await screen.findByText('Вал')

    const journalFacts = within(rowFor('Кронштейн')).getByLabelText('Факт выпуска заказа')
    expect(journalFacts).toHaveTextContent('Заказано 10')
    expect(journalFacts).toHaveTextContent('Принято Ledger 4')
    expect(journalFacts).toHaveTextContent('Осталось по заказу 6')

    fireEvent.click(rowFor('Кронштейн'))
    const detailPane = document.querySelector('.detailPane') as HTMLElement
    const cardFacts = within(detailPane).getByLabelText('Факт выпуска заказа')
    expect(cardFacts).toHaveTextContent('Заказано 10')
    expect(cardFacts).toHaveTextContent('Принято Ledger 4')
    expect(cardFacts).toHaveTextContent('Осталось по заказу 6')
  })

  it('does not present order output numbers as facts when Ledger truth is unavailable', async () => {
    renderPage()
    await screen.findByText('Вал')

    const facts = within(rowFor('Кронштейн')).getByLabelText('Факт выпуска заказа')
    expect(facts).toHaveTextContent('Заказано 10')
    expect(facts).toHaveTextContent('Принято Ledger Недоступно')
    expect(facts).toHaveTextContent('Осталось по заказу Недоступно')
    expect(facts).not.toHaveTextContent('Принято Ledger 0')
  })

  it('loads orders from the service and renders them as rows', async () => {
    const { container } = renderPage()
    await screen.findByText('Вал')

    // listProductionOrders called once on mount with paging params.
    expect(listProductionOrders).toHaveBeenCalledTimes(1)
    const params = vi.mocked(listProductionOrders).mock.calls[0][0]
    expect(params.get('limit')).toBe('100')
    expect(params.get('offset')).toBe('0')

    // Both mocked rows are visible.
    const table = ordersTable(container)
    expect(within(table).getByText('Кронштейн')).toBeInTheDocument()
    expect(within(table).getByText('Вал')).toBeInTheDocument()
    // Order numbers rendered (prodplan number for local, 1C number for 1C row).
    expect(within(table).getByText('PP-1')).toBeInTheDocument()

    // MRP run badge reflects latest_run_id.
    expect(screen.getByText('MRP run: 77')).toBeInTheDocument()
  })

  it('hydrates filters, paging, sort, and active detail from the URL', async () => {
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: fakeRows(),
      total: 202,
      limit: 100,
      offset: 100,
      latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    renderPage([
      '/production-control?search=%D0%B2%D0%B0%D0%BB&status=ready&sort_dir=desc&offset=100&active_product_id=102',
    ])
    await screen.findByText('MRP run: 77')

    const params = vi.mocked(listProductionOrders).mock.calls[0][0]
    expect(params.get('search')).toBe('вал')
    expect(params.get('status')).toBe('ready')
    expect(params.get('sort_dir')).toBe('desc')
    expect(params.get('offset')).toBe('100')
    expect(rowFor('Вал')).toHaveAttribute('aria-selected', 'true')
  })

  it('writes active detail to the URL without removing external focus params', async () => {
    const user = userEvent.setup()
    renderPage(['/production-control?product_id=101&order_id=5'])
    await screen.findByText('MRP run: 77')

    await user.click(rowFor('Вал'))
    await waitFor(() => {
      const params = new URLSearchParams(
        screen.getByTestId('location-search').textContent ?? '',
      )
      expect(params.get('product_id')).toBe('101')
      expect(params.get('order_id')).toBe('5')
      expect(params.get('active_product_id')).toBe('102')
    })
  })

  it('loads production root products from a single snapshot-driven endpoint', async () => {
    renderPage()
    await screen.findByText('Вал')

    expect(listRootProductOptions).toHaveBeenCalledWith()
    expect(listRootProductOptions).toHaveBeenCalledTimes(1)
  })

  it('restores the mechshop queue without replacing the production journal', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    expect(screen.getByRole('button', { name: 'Все заказы' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Очередь мехцеха' }))

    await waitFor(() => expect(listProductionOrders).toHaveBeenCalledTimes(2))
    const params = vi.mocked(listProductionOrders).mock.calls[1][0]
    expect(params.get('planning_contour')).toBe('mrp')
    expect(params.get('launch_source')).toBe('drum_readiness')
    expect(params.get('sort_by')).toBe('readiness_priority_key')
    expect(params.get('sort_dir')).toBe('asc')
    expect(screen.getByRole('button', { name: 'Очередь мехцеха' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('table', { name: 'Заказы на производство' })).toBeInTheDocument()
  })

  it('opens the persisted calendar drum as a separate view', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))

    expect(await screen.findByRole('table', { name: 'Календарный барабан сборки' })).toBeInTheDocument()
    expect(listDrumSchedule).toHaveBeenCalledWith(expect.any(AbortSignal))
    expect(screen.getByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Барабан сборки' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })).toHaveTextContent('Исходный план Недоступно')
    expect(screen.getByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })).toHaveTextContent('Принято Ledger Недоступно')
    expect(screen.getByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })).toHaveTextContent('Осталось выпустить Недоступно')
  })

  it('shows canonical plan output facts supplied by the drum read model without client arithmetic', async () => {
    const response = await vi.mocked(listDrumSchedule).getMockImplementation()!(new AbortController().signal)
    vi.mocked(listDrumSchedule).mockResolvedValueOnce({
      ...response,
      truth_meta: { ...response.truth_meta, truth_status: 'accepted' },
      slots: response.slots.map((slot) => ({
        ...slot,
        planned_output_qty: 12,
        accepted_plan_output_qty: 5,
        assembly_remaining_qty: 7,
      })),
    })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))

    const tile = await screen.findByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })
    expect(tile).toHaveTextContent('Исходный план 12')
    expect(tile).toHaveTextContent('Принято Ledger 5')
    expect(tile).toHaveTextContent('Осталось выпустить 7')
    await user.click(tile)
    const dialog = screen.getByRole('dialog', { name: /Плитка: Кронштейн/ })
    expect(dialog).toHaveTextContent('Принято Ledger 5')
    expect(dialog).toHaveTextContent('MRP: 77')
    expect(dialog).toHaveTextContent('Период: 2026-09-01 — 2026-09-30')
  })

  it('shows the saved deficit evidence without recalculating coverage in the browser', async () => {
    const response = await vi.mocked(listDrumSchedule).getMockImplementation()!(new AbortController().signal)
    vi.mocked(listDrumSchedule).mockResolvedValueOnce({
      ...response,
      slots: response.slots.map((slot) => ({
        ...slot,
        readiness_phase: 'blocked' as const,
        blocking_manifest: [{
          item_id: 940,
          item_code: 'CA-004940-SP',
          item_article: 'CA-004940-SP',
          item_name: 'Конёк для лыжи, чёрный',
          required_qty: '10',
          available_qty: '3',
          shortage_qty: '7',
          reason: 'SHORTAGE',
          destination_warehouse_ref1c: 'WH-ASSEMBLY',
          destination_warehouse_name: 'Склад сборки',
          path: [201, 940],
          point_of_use_qty: '1',
          custody_qty: '2',
          transit_qty: '3',
          wip_qty: '4',
          supplier_qty: '5',
          other_stock_qty: '6',
          coverage_sources: [{
            coverage_kind: 'wip_order' as const,
            qty: '4',
            source_key: 'wip:ZP-42',
            warehouse_ref1c: 'WH-WIP',
            warehouse_name: 'Склад производства',
            destination_warehouse_ref1c: 'WH-ASSEMBLY',
            destination_warehouse_name: 'Склад сборки',
            available_date: '2026-09-08',
            confidence: 'committed',
            source_kind: 'wip_order',
            source_ref: 'ЗП-000042',
          }],
        }],
      })),
    })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))

    await user.click(await screen.findByRole('button', { name: /Кронштейн: 4 шт., Пока не собирается/ }))
    const table = screen.getByRole('table', { name: 'Дефициты по источникам обеспечения' })
    const rows = within(table).getAllByRole('row')
    const cells = within(rows[1]).getAllByRole('cell').map((cell) => (cell.textContent ?? '').trim())
    expect(cells.slice(1, 9)).toEqual(['10', '1', '2', '3', '4', '5', '6', '7'])
    expect(cells[0]).toContain('CA-004940-SP')
    expect(cells[9]).toBe('Не хватает остатка')
    const sources = screen.getByLabelText('Источники покрытия: CA-004940-SP')
    expect(sources).toHaveTextContent('В производстве · 4 шт. · Склад производства → Склад сборки · ЗП-000042 · к 2026-09-08 · подтверждено документом')
    expect(screen.getByRole('link', { name: 'Журнал' })).toHaveAttribute('href', '#/production-control?search=CA-004940-SP')
    expect(screen.getByRole('link', { name: 'Очередь мехцеха' })).toHaveAttribute('href', '#/production-control?view=mechshop&search=CA-004940-SP')
  })

  it('keeps unscheduled residue collapsed and explains that it is not always a capacity gap', async () => {
    const blocker = {
      item_id: 941,
      item_code: 'FRAME-RAW',
      item_article: 'FRAME-RAW',
      item_name: 'Заготовка рамы',
      required_qty: '6',
      available_qty: '0',
      shortage_qty: '6',
      reason: 'LEAD_TIME_MISSING',
      destination_warehouse_ref1c: 'WH-ASSEMBLY',
      destination_warehouse_name: 'Склад сборки',
      path: [202, 941],
      point_of_use_qty: '0',
      custody_qty: '0',
      transit_qty: '0',
      wip_qty: '0',
      supplier_qty: '0',
      other_stock_qty: '0',
      coverage_sources: [],
    }
    vi.mocked(listDrumSchedule).mockResolvedValueOnce({
      ...await vi.mocked(listDrumSchedule).getMockImplementation()!(new AbortController().signal),
      truth_meta: { ...fakeTruthMeta, truth_status: 'accepted' },
      gaps: [{
        gap_id: 601,
        queue_line_id: 902,
        run_id: 78,
        plan_id: 1,
        plan_line_id: 12,
        period_from: '2026-09-01',
        period_to: '2026-09-30',
        item_id: 202,
        item_code: 'ART-2',
        item_name: 'Рама',
        resource_id: 1,
        gap_date: '2026-10-02',
        required_qty: 6,
        available_capacity: 0,
        gap_qty: 6,
        readiness_phase: 'blocked',
        original_priority: ['2026-08-01', 12],
        planned_output_qty: 9,
        accepted_plan_output_qty: 3,
        assembly_remaining_qty: 6,
        readiness_curve: [{
          horizon: 'now',
          cumulative_qty: '0',
          available_date: null,
          actions: [],
          required_actions: [],
          blockers: [blocker],
        }],
        action_manifest: [],
        unavailable_reasons: [],
        blocking_manifest: [blocker],
      }],
      total_gaps: 1,
    })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))

    expect(await screen.findByRole('table', { name: 'Календарный барабан сборки' })).toBeInTheDocument()
    const summary = screen.getByText('Вне календаря')
    const details = summary.closest('details')
    expect(details).not.toHaveAttribute('open')
    await user.click(summary)
    expect(details).toHaveAttribute('open')
    expect(screen.getByRole('table', { name: 'Строки барабана вне календаря' })).toBeInTheDocument()
    expect(screen.getByText('Не задан срок обеспечения')).toBeInTheDocument()
    const gapRow = screen.getByRole('table', { name: 'Строки барабана вне календаря' }).querySelector('.drumGapRow')
    expect(gapRow).toHaveTextContent('Исходный план 9')
    expect(gapRow).toHaveTextContent('Принято Ledger 3')
    expect(gapRow).toHaveTextContent('Осталось выпустить 6')
    await user.click(within(gapRow as HTMLElement).getByRole('button', { name: 'Подробнее' }))
    const dialog = screen.getByRole('dialog', { name: 'Вне календаря: Рама' })
    expect(dialog).toHaveTextContent('Сегодня можно собрать 0 из 6')
    expect(dialog).toHaveTextContent('Заготовка рамы')
    expect(dialog).toHaveTextContent('Не задан срок обеспечения')
  })

  it('shows saved queue rows without takt instead of hiding them in a metric', async () => {
    vi.mocked(listDrumSchedule).mockResolvedValueOnce({
      ...await vi.mocked(listDrumSchedule).getMockImplementation()!(new AbortController().signal),
      excluded: [{
        queue_line_id: 903,
        plan_id: 1,
        plan_line_id: 13,
        run_id: 77,
        item_id: 203,
        period_from: '2026-09-01',
        period_to: '2026-09-30',
        item_code: 'NO-TAKT',
        item_name: 'Изделие без такта',
        planned_output_qty: 12,
        accepted_plan_output_qty: 2,
        assembly_remaining_qty: 10,
        reason: 'ASSEMBLY_RATE_MISSING',
        readiness_status: 'blocked',
        readiness_curve: [],
        action_manifest: [],
        unavailable_reasons: [],
        blocking_manifest: [],
        original_priority: ['2026-08-01', 13],
      }],
      total_excluded: 1,
      total_excluded_open_qty: 10,
    })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))

    expect(await screen.findByRole('table', { name: 'Строки очереди без такта сборки' })).toBeInTheDocument()
    expect(screen.getByText('Изделие без такта')).toBeInTheDocument()
    expect(screen.getByText('Не настроен такт финишной сборки')).toBeInTheDocument()
  })

  it('moves a drum tile by mouse drag to another workday of the same resource', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))
    const tile = await screen.findByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })
    const target = screen.getByLabelText('Цех 1, 2026-09-04')
    const dataTransfer = {
      effectAllowed: 'move',
      dropEffect: 'move',
      setData: vi.fn(),
      getData: vi.fn(() => '501'),
    }

    fireEvent.dragStart(tile, { dataTransfer })
    fireEvent.dragOver(target, { dataTransfer })
    fireEvent.drop(target, { dataTransfer })

    await waitFor(() => expect(moveDrumSlot).toHaveBeenCalledWith(501, '2026-09-04', 1))
    expect(await screen.findByText('Плитка перенесена на 2026-09-04')).toBeInTheDocument()
  })

  it('keeps a rejected drum move visible instead of silently snapping back', async () => {
    vi.mocked(moveDrumSlot).mockRejectedValueOnce(new Error('Вставка сдвигает плитки за горизонт'))
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(screen.getByRole('button', { name: 'Барабан сборки' }))
    const tile = await screen.findByRole('button', { name: /Кронштейн: 4 шт., Можно собирать сейчас/ })
    const target = screen.getByLabelText('Цех 1, 2026-09-04')
    const dataTransfer = {
      effectAllowed: 'move',
      dropEffect: 'move',
      setData: vi.fn(),
      getData: vi.fn(() => '501'),
    }

    fireEvent.dragStart(tile, { dataTransfer })
    fireEvent.dragOver(target, { dataTransfer })
    fireEvent.drop(target, { dataTransfer })

    expect(await screen.findByRole('alert')).toHaveTextContent('Вставка сдвигает плитки за горизонт')
    expect(screen.getByRole('table', { name: 'Календарный барабан сборки' })).toBeInTheDocument()
  })

  it('shows shelf launch metadata in a journal row and its card', async () => {
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [{
        ...fakeRows()[0],
        source: 'mrp',
        launch_source: 'shelf_pull',
        shelf_warehouse_ref1c: 'WH-SHELF-1',
        shelf_pull_qty: 18,
        shelf_materialized_qty: 11,
        shelf_latest_start_date: '2026-07-24T07:00:00',
      }],
      total: 1,
      limit: 100,
      offset: 0,
      latest_run_id: null,
      truth_meta: fakeTruthMeta,
    })
    renderPage()

    expect(await screen.findByText('Запуск с полки')).toBeInTheDocument()
    expect(screen.getByText('WH-SHELF-1')).toBeInTheDocument()
    expect(screen.getByText('18 шт')).toBeInTheDocument()
    expect(screen.getByText('11 шт')).toBeInTheDocument()
  })

  it('auto-loads materials for the first (active) row into the detail pane', async () => {
    renderPage()
    await screen.findByText('Вал')

    // Active row = first row => saved snapshot fetched with GET on mount.
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101))
    expect(await screen.findByText('Болт М8')).toBeInTheDocument()
  })

  it('labels a direct BOM as regular components even when its basis item is named', async () => {
    vi.mocked(getOrderMaterials).mockResolvedValue({
      ...fakeMaterials(),
      coverage_basis: 'direct_bom',
      coverage_basis_item_name: 'Лыжа, кронштейн крепления, красный',
    })
    renderPage()

    expect(await screen.findByRole('heading', { name: 'Комплектующие' })).toBeInTheDocument()
    expect(screen.queryByText(/Показаны компоненты сварной детали/)).not.toBeInTheDocument()
  })

  it('does not invent warehouse coverage when material truth is unavailable', async () => {
    vi.mocked(getOrderMaterials).mockResolvedValue({
      ...fakeMaterials(),
      components: [{
        ...fakeMaterials().components[0],
        missing_qty: 0,
        coverage_status: null,
        coverage_label: null,
      }],
    })
    renderPage()

    expect(await screen.findByText('Недоступно', { selector: '.materialEta' })).toBeInTheDocument()
    expect(screen.queryByText('На складе')).not.toBeInTheDocument()
  })

  it('double-clicking a row opens saved materials without requesting a refresh', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    await user.dblClick(rowFor('Кронштейн'))
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101))
  })

  it('retries the immutable material snapshot with GET', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101))

    vi.mocked(getOrderMaterials).mockClear()
    await user.click(screen.getByRole('button', { name: 'Повторить загрузку' }))

    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledTimes(1))
    expect(getOrderMaterials).toHaveBeenCalledWith(101)
  })

  it('keeps the newest list response when an older refresh resolves last', async () => {
    const oldList = deferred<Awaited<ReturnType<typeof listProductionOrders>>>()
    const newList = deferred<Awaited<ReturnType<typeof listProductionOrders>>>()
    vi.mocked(listProductionOrders).mockReturnValueOnce(oldList.promise).mockReturnValueOnce(newList.promise)
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Сформировать' }))

    await act(async () => { newList.resolve({ rows: [fakeRows()[1]], total: 1, limit: 100, offset: 0, latest_run_id: 88, truth_meta: fakeTruthMeta }) })
    expect(await within(ordersTable(document.body)).findByText('Вал')).toBeInTheDocument()
    expect(screen.getByText('MRP run: 88')).toBeInTheDocument()
    await act(async () => { oldList.resolve({ rows: [fakeRows()[0]], total: 1, limit: 100, offset: 0, latest_run_id: 77, truth_meta: fakeTruthMeta }) })
    expect(screen.queryByText('Кронштейн')).not.toBeInTheDocument()
    expect(screen.getByText('MRP run: 88')).toBeInTheDocument()
  })

  it('keeps materials for the newest selected order when an older detail resolves last', async () => {
    const oldMaterials = deferred<MaterialsResponse>()
    const newMaterials = deferred<MaterialsResponse>()
    vi.mocked(getOrderMaterials).mockReturnValueOnce(oldMaterials.promise).mockReturnValue(newMaterials.promise)
    renderPage()
    await waitFor(() => expect(within(ordersTable(document.body)).getByText('Вал')).toBeInTheDocument())
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101))
    fireEvent.doubleClick(rowFor('Вал'))
    await act(async () => { newMaterials.resolve({ ...fakeMaterials(), order_number: 'ORD-2', item_name: 'Вал', components: [{ ...fakeMaterials().components[0], item_name: 'Новый материал' }] }) })
    expect(await screen.findByText('Новый материал')).toBeInTheDocument()
    await act(async () => { oldMaterials.resolve({ ...fakeMaterials(), components: [{ ...fakeMaterials().components[0], item_name: 'Старый материал' }] }) })
    expect(screen.queryByText('Старый материал')).not.toBeInTheDocument()
    expect(screen.getByText('Новый материал')).toBeInTheDocument()
  })

  it('changing a row status calls updateOrderStatus with product_id and new status', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    const statusSelect = within(rowFor('Кронштейн')).getByRole('combobox')
    await user.selectOptions(statusSelect, 'done')

    expect(updateOrderStatus).toHaveBeenCalledWith(101, 'done')
  })

  it('selecting a row and clicking "Запустить в 1С" posts issues then exports to 1C', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    // Export button is disabled until a row is selected.
    const exportBtn = screen.getByRole('button', { name: 'Запустить в 1С' })
    expect(exportBtn).toBeDisabled()

    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    expect(exportBtn).toBeEnabled()

    await user.click(exportBtn)

    await waitFor(() => expect(postMaterialIssues).toHaveBeenCalledWith([101], 'erp-shell', undefined))
    // issue_id 1 from postMaterialIssues result flows into the 1C export.
    await waitFor(() => expect(exportMaterialIssuesTo1C).toHaveBeenCalledWith([1]))
  })

  it('materializes a calculated MRP proposal before launching it in 1C', async () => {
    const proposal = {
      ...fakeRows()[0],
      journal_row_key: 'work-item:701',
      work_item_id: 701,
      product_id: null,
      order_id: null,
      order_number: 'MRP-R-701',
      order_prodplan_number: 'MRP-R-701',
      status: 'not_created',
      coverage_status: 'shortage',
      coverage_label: 'Дефицит',
      available_actions: ['materialize'],
      materialized_order_qty: 0,
      launchable_qty: 10,
    } as OrderRow
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [proposal], total: 1, limit: 100, offset: 0, latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    vi.mocked(getWorkItemMaterials).mockImplementation(async (_workItemId, quantity) => ({
      ...fakeMaterials(),
      product_id: null,
      work_item_id: 701,
      qty: quantity,
      components: fakeMaterials().components.map((component) => ({
        ...component,
        required_qty: component.qty_per_unit * quantity,
      })),
    }))
    vi.mocked(materializeMakeWorkItems).mockResolvedValue({
      status: 'ok',
      created: [{
        work_item_id: 701, product_id: 901, order_id: 801,
        order_number: 'MRP-R-701-1', requirement_id: 701, qty: 10,
      }],
      reused: [],
    })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Расчёт MRP · заказ ещё не создан')
    expect(within(rowFor('Кронштейн')).getByText('Не создан')).toBeInTheDocument()
    await waitFor(() => expect(getWorkItemMaterials).toHaveBeenCalledWith(701, 10, 77))

    const launchInput = screen.getByRole('spinbutton', { name: 'Количество запуска' })
    await user.clear(launchInput)
    await user.type(launchInput, '6')
    await user.tab()
    await waitFor(() => expect(getWorkItemMaterials).toHaveBeenCalledWith(701, 6, 77))
    await waitFor(() => expect(within(rowFor('Кронштейн')).getByText(/К запуску: 6 шт/)).toBeInTheDocument())
    expect(await screen.findByRole('heading', { name: 'Комплектующие на 6 шт' })).toBeInTheDocument()
    expect(screen.getByText('Нужно: 24')).toBeInTheDocument()

    await user.click(screen.getByRole('checkbox', { name: /MRP-R-701/ }))
    await user.click(screen.getByRole('button', { name: 'Запустить в 1С' }))

    await waitFor(() => expect(materializeMakeWorkItems).toHaveBeenCalledWith([{
      work_item_id: 701,
      launch_qty: 6,
      expected_materialized_qty: 0,
    }]))
    await waitFor(() => expect(postMaterialIssues).toHaveBeenCalledWith([901], 'erp-shell', undefined))
    expect(getOrderMaterials).not.toHaveBeenCalled()
  })

  it('retries proposal materials with the journal generation after refresh', async () => {
    const proposal = {
      ...fakeRows()[0],
      journal_row_key: 'work-item:701',
      work_item_id: 701,
      product_id: null,
      order_id: null,
      order_number: 'MRP-R-701',
      order_prodplan_number: 'MRP-R-701',
      status: 'not_created',
      available_actions: ['materialize'],
      materialized_order_qty: 0,
      launchable_qty: 10,
    } as OrderRow
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [proposal], total: 1, limit: 100, offset: 0, latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    vi.mocked(getWorkItemMaterials)
      .mockRejectedValueOnce(new Error('generation changed'))
      .mockResolvedValue({ ...fakeMaterials(), product_id: null, work_item_id: 701 })

    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByText(/Не удалось загрузить комплектующие: generation changed/)).toHaveAttribute('role', 'alert')
    expect(screen.queryByText('Материалы не загружены')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Обновить' }))

    await waitFor(() => expect(getWorkItemMaterials).toHaveBeenCalledTimes(2))
    expect(getWorkItemMaterials).toHaveBeenLastCalledWith(701, 10, 77)
    expect(await screen.findByText('Болт М8')).toBeInTheDocument()
  })

  it('distinguishes physical stock from negative availability and custody', async () => {
    vi.mocked(getOrderMaterials).mockResolvedValue({
      ...fakeMaterials(),
      components: [{ ...fakeMaterials().components[0], stock_qty: 318,
        reserved_qty: 492, reserved_for_order_qty: 0, available_qty: -174,
        required_qty: 400, missing_qty: 574 }],
    })
    renderPage()
    expect(await screen.findByText('Остаток: 318')).toBeInTheDocument()
    expect(screen.getByText('Под другие заказы: 492')).toBeInTheDocument()
    expect(screen.getByText('Доступно: -174')).toBeInTheDocument()
    expect(screen.getByText('Дефицит: 574')).toBeInTheDocument()
    expect(screen.queryByText('есть -174')).not.toBeInTheDocument()
  })

  it('re-quantifies a created local order and reloads its components', async () => {
    const editable = {
      ...fakeRows()[0],
      product_id: 901,
      order_id: 801,
      order_number: 'MRP-R-701-1',
      order_prodplan_number: 'MRP-R-701-1',
      status: 'created',
      issue_status: 'not_requested',
      available_actions: ['edit_quantity'],
    } as OrderRow
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [editable], total: 1, limit: 100, offset: 0, latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    vi.mocked(getOrderMaterials).mockImplementation(async () => ({
      ...fakeMaterials(),
      product_id: 901,
      qty: 14,
      components: fakeMaterials().components.map((component) => ({
        ...component,
        required_qty: component.qty_per_unit * 14,
      })),
    }))
    vi.mocked(updateOrderQuantity).mockResolvedValue({
      status: 'ok',
      product_id: 901,
      order_id: 801,
      previous_quantity: 10,
      quantity: 14,
      remaining_qty: 14,
      launchable_qty: 20,
      material_issues_open: 0,
    })

    const user = userEvent.setup()
    renderPage()
    const input = await screen.findByRole('spinbutton', { name: 'Количество запуска' })
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(901))

    await user.clear(input)
    await user.type(input, '14')
    await user.tab()

    await waitFor(() => expect(updateOrderQuantity).toHaveBeenCalledWith(901, 14))
    // Комплектация перечитывается с сервера, а не пересчитывается страницей.
    await waitFor(() => expect(vi.mocked(getOrderMaterials).mock.calls.length).toBeGreaterThan(1))
    expect(await screen.findByText('Нужно: 56')).toBeInTheDocument()
  })

  it('keeps the launch quantity read-only once the line is a fact outside', async () => {
    renderPage()
    await screen.findByText('Вал')
    expect(screen.queryByRole('spinbutton', { name: 'Количество запуска' })).toBeNull()
  })

  it.each([false, true])('asks for executors and sends explicit partial=%s', async (partial) => {
    // 1С не проводит сдельный наряд с пустой строкой регистра «Сдельные наряды»,
    // поэтому исполнители выбираются до записи, а не подставляются заглушкой.
    vi.mocked(listProductionOperations).mockResolvedValue({
      rows: [
        {
          line_number: 1, spec_id: 5, spec_operation_id: 51, operation_id: 61,
          operation_ref1c: 'op-ref-1', operation_name: 'Сборка', stage_name: 'Сборка', time_norm: 2,
        },
        {
          line_number: 2, spec_id: 5, spec_operation_id: 52, operation_id: 62,
          operation_ref1c: 'op-ref-2', operation_name: 'Контроль', stage_name: 'ОТК', time_norm: 1,
        },
      ],
      total: 2,
    } as never)

    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    const produceBtn = screen.getByRole('button', { name: 'Произвести' })
    expect(produceBtn).toBeDisabled()

    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    expect(produceBtn).toBeEnabled()

    await user.click(produceBtn)

    const dialog = await screen.findByRole('dialog')
    const submit = within(dialog).getByRole('button', { name: 'Произвести' })
    // Пока хоть одна операция без исполнителя — в 1С ничего не уходит.
    await waitFor(() => expect(submit).toBeDisabled())
    expect(produceOrderLine).not.toHaveBeenCalled()

    const selects = within(dialog).getAllByRole('combobox')
    expect(selects).toHaveLength(2)
    await user.selectOptions(selects[0], 'E1')
    expect(submit).toBeDisabled()
    await user.selectOptions(selects[1], 'E1')
    expect(submit).toBeEnabled()

    const quantity = within(dialog).getByRole('spinbutton')
    await user.clear(quantity)
    await user.type(quantity, partial ? '7' : '11')
    if (partial) await user.click(within(dialog).getByRole('checkbox', { name: 'Частичный выпуск' }))
    await user.click(submit)
    await waitFor(() => expect(produceOrderLine).toHaveBeenCalledWith(101, {
      partial,
      request_key: expect.any(String),
      qty: partial ? 7 : 11,
      operation_executors: [
        { spec_operation_id: 51, operation_id: 61, line_number: 1, employee_ref1c: 'E1' },
        { spec_operation_id: 52, operation_id: 62, line_number: 2, employee_ref1c: 'E1' },
      ],
    }))
  })

  it('shows a paint-weld pair as one row and launches both sides together', async () => {
    const [painted] = fakeRows()
    const paint = {
      ...painted,
      item_name: 'Кронштейн после окраски',
      paint_weld_pair: {
        pair_id: 7, role: 'painted', counterpart_item_id: 202,
        counterpart_item_name: 'Кронштейн после сварки', counterpart_item_article: 'WELD-1', counterpart_item_code: '',
      },
    } as OrderRow
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [paint], total: 1, limit: 100, offset: 0, latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    vi.mocked(openPaintWeldChains).mockResolvedValue({
      status: 'ok', product_ids: [101, 102], entries: [], errors: [],
    })
    const user = userEvent.setup()
    renderPage()

    await screen.findByText(/^Сварная деталь: Кронштейн после сварки/)
    expect(within(ordersTable(document.body)).getAllByRole('row')).toHaveLength(2)
    await user.click(within(rowFor('Кронштейн после окраски')).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Запустить в 1С' }))

    expect(window.confirm).toHaveBeenCalledWith(
      'Будет открыта цепочка сварка → окраска. Сначала будет запущена сварная деталь: Кронштейн после сварки. Продолжить?',
    )
    await waitFor(() => expect(openPaintWeldChains).toHaveBeenCalledWith([101]))
    await waitFor(() => expect(postMaterialIssues).toHaveBeenCalledWith([101, 102], 'erp-shell', undefined))
  })

  it('shows a welded row as a disabled chain member and identifies the welded materials basis', async () => {
    const [painted] = fakeRows()
    const paint = {
      ...painted,
      item_name: 'Кронштейн после окраски',
      paint_weld_pair: {
        pair_id: 8, role: 'painted' as const, counterpart_item_id: 202,
        counterpart_item_name: 'Кронштейн после сварки', counterpart_item_article: 'WELD-1', counterpart_item_code: '',
      },
    }
    const welded = {
      ...painted,
      product_id: 102,
      order_id: 102,
      item_id: 202,
      item_name: 'Кронштейн после сварки',
      order_number: 'WELD-1',
      order_prodplan_number: 'WELD-1',
      selection_disabled_reason: 'Запускается вместе с окрашенной деталью',
      paint_weld_pair: {
        pair_id: 8, role: 'welded' as const, counterpart_item_id: 101,
        counterpart_item_name: 'Кронштейн после окраски', counterpart_item_article: '', counterpart_item_code: '',
      },
    }
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [paint, welded], total: 2, limit: 100, offset: 0, latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    vi.mocked(getOrderMaterials).mockResolvedValue({
      ...fakeMaterials(),
      coverage_basis: 'welded_bom',
      coverage_basis_item_name: 'Кронштейн после сварки',
    })
    const user = userEvent.setup()
    renderPage()

    await waitFor(() => expect(within(ordersTable(document.body)).getByText('Кронштейн после сварки')).toBeInTheDocument())
    const tableRow = rowFor('Кронштейн после сварки')
    expect(tableRow).toHaveClass('weldedPaintWeldRow')
    expect(within(tableRow).getByRole('checkbox')).toBeDisabled()
    expect(within(ordersTable(document.body)).getByText('Цепочка сварка → окраска: запуск из окрашенной строки')).toBeInTheDocument()
    expect(within(ordersTable(document.body)).getByText(/^Цепочка сварка → окраска: сварная строка/)).toBeInTheDocument()
    expect(await screen.findByText('Показаны компоненты сварной детали: Кронштейн после сварки')).toBeInTheDocument()

    await user.click(tableRow)
    expect(screen.getByRole('button', { name: 'Произвести строку' })).toBeDisabled()

    tableRow.focus()
    await user.keyboard(' ')
    expect(within(tableRow).getByRole('checkbox')).not.toBeChecked()
  })

  it('asks for an executor on every operation of both chain sides before closing the chain', async () => {
    // Комбинированный сдельный наряд цепочки несёт строки сварки И окраски.
    // 1С не проводит наряд с пустой строкой регистра «Сдельные наряды», значит
    // правило то же, что и у обычной строки: пока хоть одна операция любой из
    // сторон без исполнителя — в 1С не уходит ничего.
    const [painted] = fakeRows()
    const paint = {
      ...painted,
      item_name: 'Кронштейн после окраски',
      paint_weld_chain: {
        role: 'painted', link_id: 72, counterpart_product_id: 102,
        counterpart_order_number: 'WELD-2', counterpart_order_prodplan_number: 'WELD-2',
        counterpart_item_name: 'Кронштейн после сварки',
        counterpart_quantity: 10, counterpart_remaining_qty: 10, counterpart_unit: 'шт',
      },
    } as OrderRow
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [paint], total: 1, limit: 100, offset: 0, latest_run_id: 77,
      truth_meta: fakeTruthMeta,
    })
    vi.mocked(listProductionOperations).mockImplementation((async (productId: number) => (
      productId === 101
        ? {
          rows: [{
            line_number: 1, spec_id: 5, spec_operation_id: 51, operation_id: 61,
            operation_ref1c: 'op-paint', operation_name: 'Покраска', stage_name: 'Окраска', time_norm: 1,
          }],
          total: 1,
        }
        : {
          rows: [{
            line_number: 1, spec_id: 6, spec_operation_id: 71, operation_id: 81,
            operation_ref1c: 'op-weld', operation_name: 'Сварка каркаса', stage_name: 'Сварка', time_norm: 2,
          }],
          total: 1,
        }
    )) as never)

    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Сварная деталь: Кронштейн после сварки')

    await user.click(within(rowFor('Кронштейн после окраски')).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Произвести' }))

    const dialog = await screen.findByRole('dialog')
    // Операции запрошены по обеим сторонам цепочки.
    await waitFor(() => expect(listProductionOperations).toHaveBeenCalledWith(101))
    expect(listProductionOperations).toHaveBeenCalledWith(102)
    expect(dialog.textContent).toContain('Сварка — Кронштейн после сварки')
    expect(dialog.textContent).toContain('Окраска — Кронштейн после окраски')

    const submit = within(dialog).getByRole('button', { name: 'Произвести' })
    await waitFor(() => expect(submit).toBeDisabled())
    expect(closePaintWeldChain).not.toHaveBeenCalled()

    // Первый блок — сварка (цепочка идёт сварка → окраска), второй — окраска.
    const selects = within(dialog).getAllByRole('combobox')
    expect(selects).toHaveLength(2)
    await user.selectOptions(selects[0], 'E1')
    expect(submit).toBeDisabled()
    expect(closePaintWeldChain).not.toHaveBeenCalled()
    await user.selectOptions(selects[1], 'E1')
    expect(submit).toBeEnabled()

    await user.click(submit)
    await waitFor(() => expect(closePaintWeldChain).toHaveBeenCalledWith(101, {
      partial: false,
      request_key: expect.any(String),
      weld_qty: expect.any(Number),
      paint_qty: expect.any(Number),
      weld_operation_executors: [
        { spec_operation_id: 71, operation_id: 81, line_number: 1, employee_ref1c: 'E1' },
      ],
      paint_operation_executors: [
        { spec_operation_id: 51, operation_id: 61, line_number: 1, employee_ref1c: 'E1' },
      ],
    }))
    expect(produceOrderLine).not.toHaveBeenCalled()
  })

  it('offers Produce without a separate close button', async () => {
    renderPage()
    await screen.findByText('Вал')
    expect(screen.queryByRole('button', { name: 'Закрыть в 1С' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Произвести' })).toBeInTheDocument()
  })

  it('"Синхронизировать" reads order completion and transfer state from 1C', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    await user.click(screen.getByRole('button', { name: 'Синхронизировать' }))

    await waitFor(() => expect(syncExecutionFrom1C).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/заказов обновлено 1, перемещений проверено 2/)).toBeInTheDocument()
  })

  it('exposes sortable state and supports keyboard activation and selection of order rows', async () => {
    const { container } = renderPage()
    await screen.findByText('Вал')
    const table = ordersTable(container)
    expect(table).toHaveAccessibleName('Заказы на производство')
    expect(within(table).getByRole('columnheader', { name: /План/ })).toHaveAttribute('aria-sort', 'ascending')

    const shaftRow = rowFor('Вал')
    shaftRow.focus()
    fireEvent.keyDown(shaftRow, { key: ' ' })
    expect(within(shaftRow).getByRole('checkbox', { name: 'Выбрать заказ ORD-2' })).toBeChecked()

    fireEvent.keyDown(shaftRow, { key: 'Enter' })
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(102))
    expect(shaftRow).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('combobox', { name: 'Статус заказа ORD-2' })).toBeInTheDocument()
  })

  it('deleting a selected local order confirms then calls deleteProductionOrder', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    // Select the local row (no 1C ref) => Delete becomes enabled.
    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    const deleteBtn = screen.getByRole('button', { name: 'Удалить' })
    expect(deleteBtn).toBeEnabled()

    await user.click(deleteBtn)

    expect(window.confirm).toHaveBeenCalled()
    await waitFor(() => expect(deleteProductionOrder).toHaveBeenCalledWith(101))
  })

  it('runs only one dangerous delete mutation when the command is double-clicked', async () => {
    const pendingDelete = deferred<Awaited<ReturnType<typeof deleteProductionOrder>>>()
    vi.mocked(deleteProductionOrder).mockReturnValueOnce(pendingDelete.promise)
    renderPage()
    await screen.findByText('Вал')
    fireEvent.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    const deleteBtn = screen.getByRole('button', { name: 'Удалить' })
    act(() => {
      fireEvent.click(deleteBtn)
      fireEvent.click(deleteBtn)
    })
    expect(window.confirm).toHaveBeenCalledOnce()
    expect(deleteProductionOrder).toHaveBeenCalledOnce()
    await act(async () => { pendingDelete.resolve({} as never) })
  })

  it('changing the status filter re-requests orders with the status param', async () => {
    const user = userEvent.setup()
    const { container } = renderPage()
    await screen.findByText('Вал')
    expect(listProductionOrders).toHaveBeenCalledTimes(1)

    // Filter bar selects: [0]=Участок, [1]=Статус, [2]=Обеспечение.
    const filterSelects = within(filterTable(container)).getAllByRole('combobox')
    await user.selectOptions(filterSelects[1], 'done')

    await waitFor(() => expect(vi.mocked(listProductionOrders).mock.calls.length).toBeGreaterThan(1))
    const params = vi.mocked(listProductionOrders).mock.calls.at(-1)![0]
    expect(params.get('status')).toBe('done')
    expect(params.get('offset')).toBe('0')
    await waitFor(() => {
      const locationParams = new URLSearchParams(
        screen.getByTestId('location-search').textContent ?? '',
      )
      expect(locationParams.get('status')).toBe('done')
      expect(locationParams.get('active_product_id')).toBe('101')
    })
  })

  it('renders an empty state when no orders are returned', async () => {
    vi.mocked(listProductionOrders).mockResolvedValue({ rows: [], total: 0, limit: 100, offset: 0, latest_run_id: null, truth_meta: fakeTruthMeta })
    renderPage()

    expect(await screen.findByText('В журнале производства нет заказов')).toBeVisible()
    expect(screen.getByText('Запрос завершён: подходящих строк нет.')).toBeVisible()
    // Detail pane placeholder appears when there is no active row.
    expect(await screen.findByText('Выберите строку')).toBeInTheDocument()
    expect(screen.queryByText('Кронштейн')).not.toBeInTheDocument()
    // No active row => materials are never fetched.
    expect(getOrderMaterials).not.toHaveBeenCalled()
  })

  it('surfaces a load error in the error line', async () => {
    vi.mocked(listProductionOrders).mockRejectedValue(new Error('boom-load-failed'))
    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent('boom-load-failed')
    expect(screen.getByText('Не удалось загрузить данные')).toBeVisible()
    expect(screen.getByText('MRP run: недоступен')).toBeVisible()
    expect(screen.getByText(/Статус истины не получен/)).toBeVisible()
    expect(screen.queryByText(/Истина недоступна/)).not.toBeInTheDocument()
  })

  it('shows stale truth only for a structured planning-truth failure', async () => {
    vi.mocked(listProductionOrders).mockRejectedValue(new ApiError(
      'accepted truth is stale',
      503,
      {
        code: 'planning_truth_unavailable',
        truth_status: 'stale',
        ledger_generation: 7,
        cutoff: '2026-08-02T08:00:00Z',
        reason: 'refresh required',
      },
    ))
    renderPage()

    expect(await screen.findByText(/Истина устарела · Ledger 7/)).toBeVisible()
    expect(screen.getByRole('alert')).toHaveTextContent('accepted truth is stale')
  })

  it('shows an explicit initial loading state before the production response arrives', async () => {
    const request = deferred<Awaited<ReturnType<typeof listProductionOrders>>>()
    vi.mocked(listProductionOrders).mockReturnValueOnce(request.promise)
    renderPage()

    expect(screen.getByText('Загрузка журнала производства…')).toBeVisible()
    expect(screen.getByText('Ответ может занять несколько секунд. Данные ещё не получены.')).toBeVisible()
    expect(screen.getByText('MRP run: загрузка…')).toBeVisible()
    expect(screen.queryByRole('table', { name: 'Заказы на производство' })).not.toBeInTheDocument()

    await act(async () => {
      request.resolve({ rows: [], total: 0, limit: 100, offset: 0, latest_run_id: null, truth_meta: fakeTruthMeta })
    })
    expect(await screen.findByText('В журнале производства нет заказов')).toBeVisible()
  })
  it('opens muted pink standalone labor dialog and sends only selected operations', async () => {
    vi.mocked(getStandalonePieceworkOptions).mockResolvedValue({
      product_id: 501, item_name: 'Сварная деталь', quantity: 10,
      operations: [
        { spec_operation_id: 51, operation_id: 61, line_number: 1, operation_name: 'Сварка' },
        { spec_operation_id: 52, operation_id: 62, line_number: 2, operation_name: 'Зачистка' },
      ],
    } as never)
    vi.mocked(createStandalonePiecework).mockResolvedValue({ status: 'ok', message: 'Наряд сварки оформлен' } as never)
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')
    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    const button = screen.getByRole('button', { name: 'Сдельный наряд' })
    expect(button).toHaveStyle({ background: '#e8c7d6' })
    await user.click(button)
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByText(/Сдельный наряд - Сварная деталь/)
    expect(within(dialog).queryByRole('checkbox', { name: 'Частичный выпуск' })).toBeNull()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Оформить операцию Зачистка' }))
    await user.selectOptions(within(dialog).getAllByRole('combobox')[0], 'E1')
    await user.clear(within(dialog).getByRole('spinbutton'))
    await user.type(within(dialog).getByRole('spinbutton'), '6')
    await user.click(within(dialog).getByRole('button', { name: 'Создать сдельный наряд' }))
    await waitFor(() => expect(createStandalonePiecework).toHaveBeenCalledWith(101, {
      qty: 6, request_key: expect.any(String),
      operation_executors: [{ spec_operation_id: 51, operation_id: 61, line_number: 1, employee_ref1c: 'E1' }],
    }))
    expect(produceOrderLine).not.toHaveBeenCalled()
    expect(closePaintWeldChain).not.toHaveBeenCalled()
  })

})
