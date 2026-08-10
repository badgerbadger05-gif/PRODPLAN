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
  listProductionEmployees: vi.fn(),
  listProductionOperations: vi.fn(),
  getProductionControlSettings: vi.fn(),
  listRootProductOptions: vi.fn(),
  materializeMakeWorkItems: vi.fn(),
  saveProductionControlSettings: vi.fn(),
  getOrderMaterials: vi.fn(),
  getWorkItemMaterials: vi.fn(),
  updateOrderStatus: vi.fn(),
  postMaterialIssues: vi.fn(),
  fetchRouteSheetsPrintHtml: vi.fn(),
  closeProductionOrder: vi.fn(),
  exportMaterialIssuesTo1C: vi.fn(),
  markMaterialIssueAssembled: vi.fn(),
  syncPostedTransfers: vi.fn(),
  produceOrderLine: vi.fn(),
  getItem: vi.fn(),
  updateItem: vi.fn(),
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
  listProductionEmployees,
  listProductionOperations,
  getOrderMaterials,
  getWorkItemMaterials,
  updateOrderStatus,
  postMaterialIssues,
  exportMaterialIssuesTo1C,
  syncPostedTransfers,
  deleteProductionOrder,
  fetchRouteSheetsPrintHtml,
  produceOrderLine,
  closeProductionOrder,
  listRootProductOptions,
  materializeMakeWorkItems,
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
      source_state_key: 'ordered',
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
  vi.mocked(closeProductionOrder).mockResolvedValue({
    status: 'ok', dry_run: false, orders_requested: 1, orders_eligible: 1,
    orders_closed: 1, orders_error: 0,
  })
  vi.mocked(syncPostedTransfers).mockResolvedValue({ candidates: 2, advanced: 0, errors: [] } as never)
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
      'Закрыть в 1С',
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
    for (const col of ['Заказ', 'Деталь', 'Кол-во', 'План', 'Участок', 'Статус', 'Обеспечение']) {
      expect(headerText).toContain(col)
    }
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

  it('uses the backend-supported journal endpoint for the mechshop view', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    expect(screen.getByRole('button', { name: 'Все заказы' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Очередь мехцеха' }))

    await waitFor(() => expect(listProductionOrders).toHaveBeenCalledTimes(2))
    const params = vi.mocked(listProductionOrders).mock.calls[1][0]
    expect(params.get('planning_contour')).toBe('mrp')
    expect(params.get('sort_by')).toBe('planned_start_date')
    expect(params.get('sort_dir')).toBe('asc')
    expect(screen.getByRole('button', { name: 'Очередь мехцеха' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Очередь мехцеха · единый журнал запуска')).toBeInTheDocument()
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

    expect(await screen.findByText('Недоступно')).toBeInTheDocument()
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
    await waitFor(() => expect(getWorkItemMaterials).toHaveBeenCalledWith(701, 10))

    const launchInput = screen.getByRole('spinbutton', { name: 'Количество запуска' })
    await user.clear(launchInput)
    await user.type(launchInput, '6')
    await user.tab()
    await waitFor(() => expect(getWorkItemMaterials).toHaveBeenCalledWith(701, 6))

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

  it('runs the canonical produce action for one assembled row', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    const produceBtn = screen.getByRole('button', { name: 'Произвести' })
    expect(produceBtn).toBeDisabled()

    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    expect(produceBtn).toBeEnabled()

    await user.click(produceBtn)
    await waitFor(() => expect(produceOrderLine).toHaveBeenCalledWith(
      101,
      { executor: 'erp-shell' },
    ))
  })

  it('runs sanctioned close-to-1C action for selected row', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    const closeBtn = screen.getByRole('button', { name: 'Закрыть в 1С' })
    expect(closeBtn).toBeDisabled()

    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    expect(closeBtn).toBeEnabled()

    await user.click(closeBtn)
    await waitFor(() => expect(closeProductionOrder).toHaveBeenCalledWith(
      101,
      { dry_run: false },
    ))
    expect(screen.getByText(/Заказ закрыт в 1С по кнопке/)).toBeInTheDocument()
  })

  it('does not confirm close when 1C export reports a partial error', async () => {
    vi.mocked(closeProductionOrder).mockResolvedValueOnce({
      status: 'partial_error', dry_run: false, orders_requested: 1, orders_eligible: 1,
      orders_closed: 0, orders_error: 1,
    })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Закрыть в 1С' }))

    expect(await screen.findByText(/Закрытие заказа в 1С не подтверждено/)).toBeInTheDocument()
    expect(screen.queryByText(/Заказ закрыт в 1С по кнопке/)).not.toBeInTheDocument()
  })

  it('"Синхронизировать" calls syncPostedTransfers and shows a summary message', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    await user.click(screen.getByRole('button', { name: 'Синхронизировать' }))

    await waitFor(() => expect(syncPostedTransfers).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/Синхронизация: проверено 2/)).toBeInTheDocument()
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
})
