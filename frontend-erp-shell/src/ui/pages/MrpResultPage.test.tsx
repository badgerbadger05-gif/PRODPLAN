import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  MrpCapacityRow,
  MrpProductionRow,
  MrpPurchaseRow,
  MrpReworkRow,
  MrpSummary,
} from '../../domain/planning'
import { downloadBase64File } from '../../lib/download'
import { getPeriodPlanMatrix } from '../../services/periodPlan'
import {
  createProductionControlOrdersFromMrp,
  exportPlanningResultProduction,
  exportPlanningResultPurchases,
  exportPlanningResultRework,
  exportPurchasesTo1C,
  getPlanningResultCapacity,
  getPlanningResultProduction,
  getPlanningResultPurchases,
  getPlanningResultRework,
  getPlanningRunSummary,
} from '../../services/planning'
import { MrpResultPage } from './MrpResultPage'

vi.mock('../../lib/download', () => ({ downloadBase64File: vi.fn() }))
vi.mock('../../services/periodPlan', () => ({ getPeriodPlanMatrix: vi.fn() }))
vi.mock('../../services/planning', () => ({
  createProductionControlOrdersFromMrp: vi.fn(),
  exportPlanningResultProduction: vi.fn(),
  exportPlanningResultPurchases: vi.fn(),
  exportPlanningResultRework: vi.fn(),
  exportPurchasesTo1C: vi.fn(),
  getPlanningResultCapacity: vi.fn(),
  getPlanningResultProduction: vi.fn(),
  getPlanningResultPurchases: vi.fn(),
  getPlanningResultRework: vi.fn(),
  getPlanningRunSummary: vi.fn(),
}))

const summary: MrpSummary = {
  run: {
    run_id: 41,
    status: 'SUCCESS',
    started_at: '2026-07-20T08:30:00Z',
    finished_at: '2026-07-20T08:31:00Z',
    horizon_days: 30,
    source_plan_id: 7,
  },
  counts: {
    production_orders: 3,
    purchase_requests: 3,
    rework_requests: 1,
  },
  capacity: { overloaded_buckets: 1 },
}

const productionRows: MrpProductionRow[] = [
  {
    order_id: 1001,
    source_order_ids: [101, 102],
    item_id: 501,
    item_name: 'Насос ГА-1',
    item_article: 'НАС-01',
    unit: 'шт',
    qty: 5,
    need_date: '2026-07-25',
    start_date: '2026-07-21',
    finish_date: '2026-07-24',
    main_area_name: 'Сборка',
    norm_hours_total: 8,
  },
  {
    order_id: 1002,
    source_order_ids: [103],
    item_id: 502,
    item_name: 'Нулевая потребность',
    item_article: 'НУЛЬ',
    unit: 'шт',
    qty: 0,
  },
]

const purchaseRows: MrpPurchaseRow[] = [
  {
    purchase_id: 2001,
    source_purchase_ids: [201, 202],
    item_id: 601,
    item_name: 'Подшипник',
    item_article: 'ПД-01',
    unit: 'шт',
    qty: 10,
    supplier_ref1c: 'supplier-a',
    supplier_name: 'ООО Альфа',
    category_id: 11,
    category_name: 'Комплектующие',
  },
  {
    purchase_id: 2002,
    source_purchase_ids: [203],
    item_id: 602,
    item_name: 'Лист стальной',
    item_article: 'ЛИСТ-01',
    unit: 'кг',
    qty: 20,
    supplier_ref1c: 'supplier-b',
    supplier_name: 'ООО Бета',
    category_ref1c: 'category-metal',
    category_name: 'Металл',
  },
  {
    purchase_id: 2003,
    item_id: 603,
    item_name: 'Позиция без поставщика',
    item_article: 'БП-01',
    unit: 'шт',
    qty: 1,
    supplier_ref1c: 'missing-name-ref',
    supplier_name: ' ',
  },
]

const reworkRows: MrpReworkRow[] = [{
  rework_id: 3001,
  item_id: 701,
  item_name: 'Корпус на доработку',
  qty: 2,
  requested_qty: 3,
  planned_qty: 1,
}]

const capacityRows: MrpCapacityRow[] = [{
  area_id: 9,
  bucket_date: '2026-07-22',
  hours_planned: 12,
  hours_available: 8,
  overload_hours: 4,
}]

function paged<T>(rows: T[], total = rows.length, offset = 0) {
  return { rows, total, limit: 200, offset }
}

function renderPage(entry = '/mrp-runs/41') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/mrp-runs/:runId" element={<MrpResultPage />} />
        <Route path="/mrp-runs" element={<div>Список прогонов</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('MrpResultPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getPlanningRunSummary).mockResolvedValue(summary)
    vi.mocked(getPlanningResultProduction).mockResolvedValue(paged(productionRows, 450))
    vi.mocked(getPlanningResultPurchases).mockResolvedValue(paged(purchaseRows))
    vi.mocked(getPlanningResultRework).mockResolvedValue(paged(reworkRows))
    vi.mocked(getPlanningResultCapacity).mockResolvedValue(paged(capacityRows))
    vi.mocked(getPeriodPlanMatrix).mockResolvedValue({
      plan: {
        id: 7,
        name: 'План июля',
        status: 'fixed',
        period_from: '2026-07-01',
        period_to: '2026-07-31',
      },
      buckets: [],
      rows: [{
        item_id: 501,
        item_code: 'PUMP-01',
        item_name: 'Насос ГА-1',
        item_article: 'НАС-01',
        total_qty: 5,
        buckets: {},
        locked_buckets: {},
      }],
      total: 1,
    })
    vi.mocked(createProductionControlOrdersFromMrp).mockResolvedValue({ created: 2 })
    vi.mocked(exportPurchasesTo1C).mockResolvedValue({ exported: 2 })
    const exported = {
      data_base64: 'WA==',
      filename: 'mrp.xlsx',
      content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }
    vi.mocked(exportPlanningResultProduction).mockResolvedValue(exported)
    vi.mocked(exportPlanningResultPurchases).mockResolvedValue(exported)
    vi.mocked(exportPlanningResultRework).mockResolvedValue(exported)
  })

  it('loads summary, initial production and root options with exact parameters', async () => {
    renderPage()

    expect(await screen.findByText('Насос ГА-1')).toBeVisible()
    expect(screen.getByText('Успешно')).toBeVisible()
    expect(getPlanningRunSummary).toHaveBeenCalledWith(41)
    expect(getPlanningResultProduction).toHaveBeenCalledWith(41, {
      date_from: undefined,
      date_to: undefined,
      root_item_id: null,
      limit: 200,
      offset: 0,
    })
    await waitFor(() => expect(getPeriodPlanMatrix).toHaveBeenCalledWith(7))

    await userEvent.setup().click(screen.getByRole('button', { name: 'Корневое изделие' }))
    expect(screen.getByRole('option', { name: 'Насос ГА-1 · НАС-01' })).toBeVisible()
  })

  it('loads tabs lazily once and honors the query tab and highlighted row', async () => {
    const user = userEvent.setup()
    renderPage('/mrp-runs/41?tab=purchases&purchase_id=202')

    expect(await screen.findByText('Подшипник')).toBeVisible()
    expect(getPlanningResultProduction).not.toHaveBeenCalled()
    expect(getPlanningResultPurchases).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Подшипник').closest('tr')).toHaveClass('activeRow')

    await user.click(screen.getByRole('button', { name: 'Мощности' }))
    expect(await screen.findByText('Участок #9')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Закупки' }))
    expect(screen.getByText('Подшипник')).toBeVisible()
    expect(getPlanningResultPurchases).toHaveBeenCalledTimes(1)
  })

  it('invalidates tabs for date and root filters and pages with the active offset', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')

    await user.type(screen.getByLabelText('С'), '2026-07-21')
    await user.type(screen.getByLabelText('По'), '2026-07-31')
    await user.click(screen.getByRole('button', { name: 'Сформировать' }))
    await waitFor(() => expect(getPlanningResultProduction).toHaveBeenLastCalledWith(41, {
      date_from: '2026-07-21',
      date_to: '2026-07-31',
      root_item_id: null,
      limit: 200,
      offset: 0,
    }))

    await user.click(screen.getByRole('button', { name: 'Корневое изделие' }))
    await user.selectOptions(screen.getByRole('combobox'), '501')
    await waitFor(() => expect(getPlanningResultProduction).toHaveBeenLastCalledWith(41, {
      date_from: '2026-07-21',
      date_to: '2026-07-31',
      root_item_id: 501,
      limit: 200,
      offset: 0,
    }))

    await user.click(screen.getByRole('button', { name: 'Вперед' }))
    await waitFor(() => expect(getPlanningResultProduction).toHaveBeenLastCalledWith(41, {
      date_from: '2026-07-21',
      date_to: '2026-07-31',
      root_item_id: 501,
      limit: 200,
      offset: 200,
    }))
  })

  it('uses aggregated production IDs and excludes zero-quantity rows from selection', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')

    const selectable = screen.getByRole('checkbox', { name: 'Выбрать Насос ГА-1' })
    const zero = screen.getByRole('checkbox', { name: 'Выбрать Нулевая потребность' })
    expect(zero).toBeDisabled()
    await user.click(selectable)
    expect(screen.getByRole('button', { name: 'Создать заказы (2)' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Создать заказы (2)' }))

    expect(createProductionControlOrdersFromMrp).toHaveBeenCalledWith({
      run_id: 41,
      date_from: undefined,
      date_to: undefined,
      planned_order_ids: [101, 102],
    })
    expect(await screen.findByText('Создание заказов: выполнено, новых 2')).toBeVisible()
    expect(getPlanningRunSummary).toHaveBeenCalledTimes(2)
    expect(getPlanningResultProduction).toHaveBeenCalledTimes(2)
  })

  it('keeps page-local purchase filters and exports aggregated IDs with the exact 1C flags', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')
    await user.click(screen.getByRole('button', { name: 'Закупки' }))
    await screen.findByText('Подшипник')

    expect(screen.getByRole('option', { name: 'Без наименования' })).toBeVisible()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Фильтр по поставщику' }), 'supplier-a')
    expect(screen.getByText('Подшипник')).toBeVisible()
    expect(screen.queryByText('Лист стальной')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Фильтр по категории' }), '11')
    expect(screen.getByText('Подшипник')).toBeVisible()

    await user.click(screen.getByRole('checkbox', { name: 'Выбрать Подшипник' }))
    await user.click(screen.getByRole('button', { name: 'Выгрузить в 1С (2)' }))
    expect(exportPurchasesTo1C).toHaveBeenCalledWith(41, {
      date_from: undefined,
      date_to: undefined,
      purchase_ids: [201, 202],
      dry_run: false,
      allow_production: true,
    })
    expect(await screen.findByText('Выгрузка закупок в 1С: выполнено, новых 2')).toBeVisible()
  })

  it('dispatches XLSX export by active tab and downloads each response', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'XLSX' }))
    expect(exportPlanningResultProduction).toHaveBeenCalledWith(41, {
      format: 'xlsx',
      date_from: undefined,
      date_to: undefined,
      root_item_id: null,
    })

    await user.click(screen.getByRole('button', { name: 'Закупки' }))
    await screen.findByText('Подшипник')
    await user.click(screen.getByRole('button', { name: 'XLSX' }))
    expect(exportPlanningResultPurchases).toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Переработка' }))
    await screen.findByText('Корпус на доработку')
    await user.click(screen.getByRole('button', { name: 'XLSX' }))
    expect(exportPlanningResultRework).toHaveBeenCalled()
    expect(downloadBase64File).toHaveBeenCalledTimes(3)

    await user.click(screen.getByRole('button', { name: 'Мощности' }))
    await screen.findByText('Участок #9')
    expect(screen.queryByRole('button', { name: 'XLSX' })).not.toBeInTheDocument()
  })
})
