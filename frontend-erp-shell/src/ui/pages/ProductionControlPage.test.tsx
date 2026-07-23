import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

import { ProductionControlPage } from './ProductionControlPage'
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
  saveProductionControlSettings: vi.fn(),
  getOrderMaterials: vi.fn(),
  updateOrderStatus: vi.fn(),
  postMaterialIssues: vi.fn(),
  fetchRouteSheetsPrintHtml: vi.fn(),
  exportMaterialIssuesTo1C: vi.fn(),
  markMaterialIssueAssembled: vi.fn(),
  syncPostedTransfers: vi.fn(),
  closePaintWeldChain: vi.fn(),
  updateOrderQuantity: vi.fn(),
  produceOrder: vi.fn(),
  exportManufacturesTo1C: vi.fn(),
  exportManufacturesPieceworkTo1C: vi.fn(),
  rollbackManufactureLocal: vi.fn(),
  createOrdersFromMrpRequirements: vi.fn(),
  getItem: vi.fn(),
  updateItem: vi.fn(),
  deleteProductionOrder: vi.fn(),
}))

vi.mock('../../services/periodPlan', () => ({
  listPeriodPlans: vi.fn(),
  getPeriodPlanMatrix: vi.fn(),
}))

vi.mock('../../services/resources', () => ({
  listResources: vi.fn(),
}))

import {
  listProductionOrders,
  listProductionEmployees,
  listProductionOperations,
  getOrderMaterials,
  updateOrderStatus,
  postMaterialIssues,
  exportMaterialIssuesTo1C,
  syncPostedTransfers,
  deleteProductionOrder,
  fetchRouteSheetsPrintHtml,
} from '../../services/productionControl'
import { listPeriodPlans, getPeriodPlanMatrix } from '../../services/periodPlan'
import { listResources } from '../../services/resources'

// --- Fake data shaped to the domain types ---------------------------------
// row 101: local order (no 1C ref), coverage 'assembled' + issue 'posted'
//   => produceable AND deletable.
// row 102: opened in 1C (has order_ref1c) => not deletable, shortage.
function fakeRows(): OrderRow[] {
  return [
    {
      product_id: 101,
      item_id: 201,
      order_number: 'ORD-1',
      order_prodplan_number: 'PP-1',
      order_source: 'mrp',
      source: 'mrp',
      order_ref1c: null,
      item_name: 'Кронштейн',
      item_article: 'ART-1',
      unit: 'шт',
      quantity: 10,
      produced_qty: 0,
      remaining_qty: 10,
      status: 'ready',
      coverage_status: 'assembled',
      coverage_label: 'Собрано',
      issue_status: 'posted',
      workshop_name: 'Цех 1',
    },
    {
      product_id: 102,
      item_id: 202,
      order_number: 'ORD-2',
      order_source: '1c',
      order_ref1c: 'REF-2',
      order_one_c_number: '1C-2',
      item_name: 'Вал',
      item_article: 'ART-2',
      unit: 'шт',
      quantity: 5,
      produced_qty: 0,
      remaining_qty: 5,
      status: 'shortage',
      coverage_status: 'shortage',
      workshop_name: 'Цех 2',
    },
  ]
}

function fakeMaterials(): MaterialsResponse {
  return {
    order_number: 'ORD-1',
    item_name: 'Кронштейн',
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
      },
    ],
  }
}

const fakeResources: ProductionResource[] = [
  { resource_id: 1, resource_name: 'Цех 1' },
  { resource_id: 2, resource_name: 'Цех 2' },
]

function renderPage(initialEntries: string[] = ['/production-control']) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <ProductionControlPage />
    </MemoryRouter>,
  )
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
  })
  vi.mocked(getOrderMaterials).mockResolvedValue(fakeMaterials())
  vi.mocked(listResources).mockResolvedValue(fakeResources)
  vi.mocked(listPeriodPlans).mockResolvedValue({ rows: [] } as never)
  vi.mocked(getPeriodPlanMatrix).mockResolvedValue({ rows: [] } as never)
  vi.mocked(listProductionEmployees).mockResolvedValue({
    rows: [{ employee_id: 1, employee_ref1c: 'E1', employee_name: 'Иванов' }],
    total: 1,
  })
  vi.mocked(listProductionOperations).mockResolvedValue({ rows: [], total: 0 })
  vi.mocked(updateOrderStatus).mockResolvedValue({} as never)
  vi.mocked(deleteProductionOrder).mockResolvedValue({} as never)
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
  vi.mocked(syncPostedTransfers).mockResolvedValue({ candidates: 2, advanced: 0, errors: [] } as never)
})

describe('ProductionControlPage — characterization', () => {
  it('renders the page shell: heading, command bar, and table columns', async () => {
    const { container } = renderPage()
    await screen.findByText('Вал')

    // Breadcrumb + document heading
    expect(screen.getByRole('heading', { name: 'Журнал заказов на производство' })).toBeInTheDocument()
    expect(screen.getByText('Производство / Журнал заказов на производство')).toBeInTheDocument()

    // Command bar buttons
    for (const label of [
      'Запустить в 1С',
      'Произвести',
      'Закрыть цепочку',
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

  it('uses the existing journal endpoint for the mechshop DBR view', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    expect(screen.getByRole('button', { name: 'Все заказы' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Очередь мехцеха' }))

    await waitFor(() => expect(listProductionOrders).toHaveBeenCalledTimes(2))
    const params = vi.mocked(listProductionOrders).mock.calls[1][0]
    expect(params.get('planning_contour')).toBe('dbr_feeder')
    expect(params.get('sort_by')).toBe('dbr_priority')
    expect(params.get('sort_dir')).toBe('desc')
    expect(screen.getByRole('button', { name: 'Очередь мехцеха' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Приоритет DBR · единый журнал запуска')).toBeInTheDocument()
  })

  it('shows DBR priority in a journal row and planning provenance in its card', async () => {
    vi.mocked(listProductionOrders).mockResolvedValue({
      rows: [{
        ...fakeRows()[0],
        source: 'dbr',
        source_dbr_signal_id: 431,
        planning: {
          contour: 'dbr_feeder',
          slot_id: 912,
          signal_type: 'Цепочка',
          priority: 1.42,
          zone: 'red',
          required_date: '2026-07-24',
          queue_state: 'ready',
          reason: 'Запуск по слоту барабана',
        },
      }],
      total: 1,
      limit: 100,
      offset: 0,
    })
    renderPage()

    expect(await screen.findByText('DBR 1.42')).toBeInTheDocument()
    expect(screen.getByText('Планирование DBR')).toBeInTheDocument()
    expect(screen.getByText('#431')).toBeInTheDocument()
    expect(screen.getByText('#912')).toBeInTheDocument()
    expect(screen.getByText('Запуск по слоту барабана')).toBeInTheDocument()
  })

  it('auto-loads materials for the first (active) row into the detail pane', async () => {
    renderPage()
    await screen.findByText('Вал')

    // Active row = first row => materials fetched with refresh=false on mount.
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101, false))
    expect(await screen.findByText('Болт М8')).toBeInTheDocument()
  })

  it('double-clicking a row refetches its materials with refresh=true', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    await user.dblClick(rowFor('Кронштейн'))
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101, true))
  })

  it('keeps the newest list response when an older refresh resolves last', async () => {
    const oldList = deferred<Awaited<ReturnType<typeof listProductionOrders>>>()
    const newList = deferred<Awaited<ReturnType<typeof listProductionOrders>>>()
    vi.mocked(listProductionOrders).mockReturnValueOnce(oldList.promise).mockReturnValueOnce(newList.promise)
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Сформировать' }))

    await act(async () => { newList.resolve({ rows: [fakeRows()[1]], total: 1, limit: 100, offset: 0, latest_run_id: 88 }) })
    expect(await within(ordersTable(document.body)).findByText('Вал')).toBeInTheDocument()
    expect(screen.getByText('MRP run: 88')).toBeInTheDocument()
    await act(async () => { oldList.resolve({ rows: [fakeRows()[0]], total: 1, limit: 100, offset: 0, latest_run_id: 77 }) })
    expect(screen.queryByText('Кронштейн')).not.toBeInTheDocument()
    expect(screen.getByText('MRP run: 88')).toBeInTheDocument()
  })

  it('keeps materials for the newest selected order when an older detail resolves last', async () => {
    const oldMaterials = deferred<MaterialsResponse>()
    const newMaterials = deferred<MaterialsResponse>()
    vi.mocked(getOrderMaterials).mockReturnValueOnce(oldMaterials.promise).mockReturnValue(newMaterials.promise)
    renderPage()
    await waitFor(() => expect(within(ordersTable(document.body)).getByText('Вал')).toBeInTheDocument())
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(101, false))
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

  it('opens the produce dialog for a single assembled/posted row', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Вал')

    const produceBtn = screen.getByRole('button', { name: 'Произвести' })
    expect(produceBtn).toBeDisabled()

    await user.click(within(rowFor('Кронштейн')).getByRole('checkbox'))
    expect(produceBtn).toBeEnabled()

    await user.click(produceBtn)

    expect(await screen.findByRole('dialog', { name: 'Произвести - Кронштейн' })).toBeInTheDocument()
    expect(screen.getByLabelText('Количество (шт)')).toBeInTheDocument()
    // Dialog bootstraps operations + employees for the selected product.
    await waitFor(() => expect(listProductionOperations).toHaveBeenCalledWith(101))
    expect(listProductionEmployees).toHaveBeenCalled()
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
    await waitFor(() => expect(getOrderMaterials).toHaveBeenCalledWith(102, true))
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
  })

  it('renders an empty state when no orders are returned', async () => {
    vi.mocked(listProductionOrders).mockResolvedValue({ rows: [], total: 0, limit: 100, offset: 0, latest_run_id: null })
    renderPage()

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
  })
})
