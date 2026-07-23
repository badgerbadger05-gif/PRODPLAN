import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { PeriodPlanPage } from './PeriodPlanPage'
import type {
  ExecutionJournalResponse,
  PeriodPlan,
  PeriodPlanMatrix,
} from '../../domain/planning'
import type { NomenclatureSearchItem } from '../../domain/productionPlan'
import * as periodPlanSvc from '../../services/periodPlan'
import * as productionPlanSvc from '../../services/productionPlan'

// ── Mock the entire service layer so no real network happens ──────────────────
vi.mock('../../services/periodPlan', () => ({
  listPeriodPlans: vi.fn(),
  createPeriodPlan: vi.fn(),
  updatePeriodPlanHeader: vi.fn(),
  archivePeriodPlan: vi.fn(),
  unarchivePeriodPlan: vi.fn(),
  listPeriodPlanRuns: vi.fn(),
  fixPeriodPlan: vi.fn(),
  createMrpSnapshot: vi.fn(),
  getPeriodPlanMatrix: vi.fn(),
  bulkUpsertPeriodPlanLines: vi.fn(),
  getExecutionJournal: vi.fn(),
  deletePeriodPlan: vi.fn(),
  addItemToPeriodPlan: vi.fn(),
  deleteItemFromPeriodPlan: vi.fn(),
  reconcileRun: vi.fn(),
  createProductionOrdersFromRequirements: vi.fn(),
}))
vi.mock('../../services/productionPlan', () => ({
  searchNomenclature: vi.fn(),
  ensurePlanItem: vi.fn(),
}))

// ── Fake data shaped to the domain types ──────────────────────────────────────
const draftPlan: PeriodPlan = {
  id: 123,
  name: 'МАЙ 2026',
  status: 'draft',
  period_from: '2026-05-01',
  period_to: '2026-05-29',
  comment: 'тестовый план',
  created_by: 'ivan',
  created_at: '2026-04-01T10:00:00',
  fixed_at: null,
  fixed_by: null,
  line_count: 1,
}

const fixedPlan: PeriodPlan = {
  ...draftPlan,
  status: 'fixed',
  fixed_at: '2026-04-05T12:00:00',
  fixed_by: 'ivan',
}

function makeMatrix(plan: PeriodPlan): PeriodPlanMatrix {
  return {
    plan,
    buckets: ['2026-05-01', '2026-05-08'],
    rows: [
      {
        item_id: 501,
        item_code: 'C-501',
        item_name: 'Насос ГА-1',
        item_article: 'ART-501',
        total_qty: 10,
        buckets: { '2026-05-01': 4, '2026-05-08': 6 },
        locked_buckets: {},
      },
    ],
    total: 10,
  }
}

const journalResponse: ExecutionJournalResponse = {
  plan: fixedPlan,
  run_id: 900,
  truth_status: 'accepted',
  ledger_generation: 7,
  cutoff: '2026-05-31T23:59:59Z',
  rows: [
    {
      req_id: 1,
      item_id: 501,
      item_code: 'C-501',
      item_article: 'ART-501',
      item_name: 'Насос ГА-1',
      flow: 'production',
      bom_level: 0,
      gross_qty: 10,
      stock_qty: 0,
      net_qty: 10,
      ordered_qty: 4,
      completed_qty: 2,
      covered_qty: 2,
      remaining_qty: 8,
      unassigned_qty: 0,
      coverage_pct: 20,
      need_date: '2026-05-15',
      work_items: [],
    },
  ],
  summary: {
    total_items: 1,
    fully_covered: 0,
    partially_covered: 1,
    not_covered: 0,
    net_zero: 0,
  },
}

const listPlanA: PeriodPlan = {
  id: 101,
  name: 'АПРЕЛЬ 2026',
  status: 'draft',
  period_from: '2026-04-01',
  period_to: '2026-04-30',
  comment: null,
  created_by: 'ivan',
  created_at: '2026-03-01T09:00:00',
  fixed_at: null,
  fixed_by: null,
  line_count: 3,
}
const listPlanB: PeriodPlan = {
  ...listPlanA,
  id: 102,
  name: 'МАРТ 2026',
  status: 'archived',
}

const searchItem: NomenclatureSearchItem = {
  item_id: 777,
  item_code: 'C-909',
  item_name: 'Клапан КР-9',
  item_article: 'ART-909',
  similarity: 0.87,
}

function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/period-plan" element={<PeriodPlanPage />} />
        <Route path="/period-plan/:planId" element={<PeriodPlanPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  // confirm() is used by delete flows; jsdom's default is unimplemented.
  vi.stubGlobal('confirm', vi.fn(() => true))

  vi.mocked(periodPlanSvc.listPeriodPlans).mockResolvedValue({
    rows: [listPlanA, listPlanB],
    total: 2,
  })
  vi.mocked(periodPlanSvc.getPeriodPlanMatrix).mockResolvedValue(makeMatrix(draftPlan))
  vi.mocked(periodPlanSvc.listPeriodPlanRuns).mockResolvedValue({ rows: [], total: 0 })
  vi.mocked(periodPlanSvc.getExecutionJournal).mockResolvedValue(journalResponse)
  vi.mocked(periodPlanSvc.createPeriodPlan).mockResolvedValue({ ...draftPlan, id: 555 })
  vi.mocked(periodPlanSvc.deletePeriodPlan).mockResolvedValue({ status: 'ok', id: 101, name: 'АПРЕЛЬ 2026' })
  vi.mocked(periodPlanSvc.deleteItemFromPeriodPlan).mockResolvedValue({ status: 'ok', plan_id: 123, item_id: 501, deleted: 1 })
  vi.mocked(periodPlanSvc.addItemToPeriodPlan).mockResolvedValue({ status: 'ok', plan_id: 123, item_id: 777 })
  vi.mocked(periodPlanSvc.createMrpSnapshot).mockResolvedValue({
    status: 'ok', run_id: 900, plan_id: 123, requirement_count: 5, purchase_count: 2, rework_count: 1,
  })
  vi.mocked(productionPlanSvc.searchNomenclature).mockResolvedValue({
    items: [searchItem], total: 1, query: '', search_type: 'trgm',
  })
  vi.mocked(productionPlanSvc.ensurePlanItem).mockResolvedValue({ status: 'ok', item_id: 777 })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── List view ─────────────────────────────────────────────────────────────────
describe('PeriodPlanPage — list view', () => {
  it('renders list chrome and loads plans from the service', async () => {
    renderAt('/period-plan')

    // Data load fires listPeriodPlans and rows become visible.
    expect(await screen.findByText('АПРЕЛЬ 2026')).toBeInTheDocument()
    expect(screen.getByText('МАРТ 2026')).toBeInTheDocument()
    expect(periodPlanSvc.listPeriodPlans).toHaveBeenCalled()

    // Key chrome: breadcrumb, primary action, sortable column headers.
    expect(screen.getByText('Планирование / Планирование выпуска')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Новый план' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Название/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Статус/ })).toBeInTheDocument()
  })

  it('shows the empty state when no plans are returned', async () => {
    vi.mocked(periodPlanSvc.listPeriodPlans).mockResolvedValue({ rows: [], total: 0 })
    renderAt('/period-plan')

    expect(await screen.findByText('Нет планов')).toBeInTheDocument()
  })

  it('creates a plan through the create form and calls createPeriodPlan with entered values', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan')
    await screen.findByText('АПРЕЛЬ 2026')

    await user.click(screen.getByRole('button', { name: 'Новый план' }))
    const nameInput = screen.getByPlaceholderText('Например: МАЙ 2026')
    await user.type(nameInput, 'ИЮНЬ 2026')
    await user.click(screen.getByRole('button', { name: 'Создать' }))

    await waitFor(() => expect(periodPlanSvc.createPeriodPlan).toHaveBeenCalled())
    expect(periodPlanSvc.createPeriodPlan).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'ИЮНЬ 2026', comment: null }),
    )
  })

  it('double-clicking a row navigates to the plan detail (useNavigate)', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan')
    const row = await screen.findByText('АПРЕЛЬ 2026')

    await user.dblClick(row)

    // Navigation renders the detail view which loads the matrix for that id.
    await waitFor(() => expect(periodPlanSvc.getPeriodPlanMatrix).toHaveBeenCalledWith(101))
    expect(await screen.findByText('Код')).toBeInTheDocument()
  })

  it('opens the selected plan with Enter exactly once', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan')
    const plan = await screen.findByText('АПРЕЛЬ 2026')
    await user.click(plan)

    const event = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
    })
    fireEvent(document, event)

    await waitFor(() => expect(periodPlanSvc.getPeriodPlanMatrix).toHaveBeenCalledWith(101))
    expect(event.defaultPrevented).toBe(true)

    // The list shortcut listener must be gone after the route switches to detail.
    await screen.findByText('Код')
    vi.mocked(periodPlanSvc.getPeriodPlanMatrix).mockClear()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(periodPlanSvc.getPeriodPlanMatrix).not.toHaveBeenCalled()
  })

  it('keeps Enter local to inputs while F5 still reloads from an input', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan')
    const plan = await screen.findByText('АПРЕЛЬ 2026')
    await user.click(plan)
    const author = screen.getByPlaceholderText('любая часть имени')
    author.focus()
    vi.mocked(periodPlanSvc.listPeriodPlans).mockClear()

    fireEvent.keyDown(author, { key: 'Enter' })
    expect(periodPlanSvc.getPeriodPlanMatrix).not.toHaveBeenCalled()

    const refresh = new KeyboardEvent('keydown', {
      key: 'F5',
      bubbles: true,
      cancelable: true,
    })
    fireEvent(author, refresh)

    await waitFor(() => expect(periodPlanSvc.listPeriodPlans).toHaveBeenCalledTimes(1))
    expect(refresh.defaultPrevented).toBe(true)
    expect(screen.getByText('Планирование / Планирование выпуска')).toBeInTheDocument()
  })
})

// ── Detail view ───────────────────────────────────────────────────────────────
describe('PeriodPlanPage — detail view', () => {
  it('loads the matrix for the route param and renders header + rows', async () => {
    renderAt('/period-plan/123')

    await waitFor(() => expect(periodPlanSvc.getPeriodPlanMatrix).toHaveBeenCalledWith(123))
    expect(periodPlanSvc.listPeriodPlanRuns).toHaveBeenCalledWith(123)

    // Matrix column headers and the mocked row appear.
    expect(await screen.findByText('Насос ГА-1')).toBeInTheDocument()
    expect(screen.getByText('Код')).toBeInTheDocument()
    expect(screen.getByText('Номенклатура')).toBeInTheDocument()
    expect(screen.getByText('Итого')).toBeInTheDocument()
    // Plan name surfaces in the document window title.
    expect(screen.getAllByText('МАЙ 2026').length).toBeGreaterThan(0)
  })

  it('runs nomenclature search while typing in the draft search box', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    const input = await screen.findByPlaceholderText(/поиск по мере ввода/)

    await user.type(input, 'кла')

    await waitFor(() => expect(productionPlanSvc.searchNomenclature).toHaveBeenCalledWith('кла'))
    // Result appears in the suggestion dropdown.
    expect(await screen.findByText('Клапан КР-9')).toBeInTheDocument()
  })

  it('adds a search result via ensurePlanItem then addItemToPeriodPlan', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    const input = await screen.findByPlaceholderText(/поиск по мере ввода/)

    await user.type(input, 'кла')
    const option = await screen.findByRole('button', { name: /Клапан КР-9/ })
    await user.click(option)

    await waitFor(() => expect(productionPlanSvc.ensurePlanItem).toHaveBeenCalledWith(searchItem))
    expect(periodPlanSvc.addItemToPeriodPlan).toHaveBeenCalledWith(123, 777)
  })

  it('switches to the journal tab and loads the execution journal', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'Журнал исполнения' }))

    await waitFor(() => expect(periodPlanSvc.getExecutionJournal).toHaveBeenCalled())
    expect(vi.mocked(periodPlanSvc.getExecutionJournal).mock.calls[0][0]).toBe(123)
    // Journal-specific column header appears.
    expect(await screen.findByText('Тип')).toBeInTheDocument()
  })

  it('fails closed when execution truth is unavailable', async () => {
    vi.mocked(periodPlanSvc.getExecutionJournal).mockResolvedValue({
      ...journalResponse,
      truth_status: 'uninitialized',
      truth_reason: 'Исторические движения Ledger не загружены',
      ledger_generation: null,
      cutoff: null,
    })
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'Журнал исполнения' }))

    expect(await screen.findByText(/Исполнение не рассчитано\/недоступно/)).toBeInTheDocument()
    expect(screen.getByText(/Исторические движения Ledger не загружены/)).toBeInTheDocument()
    expect(screen.queryByText(/Общее выполнение:/)).not.toBeInTheDocument()
    expect(screen.queryByText('20%')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Создать заказы производства' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'CSV' })).toBeDisabled()
  })

  it('deletes a matrix row (draft) via deleteItemFromPeriodPlan after confirm', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    await screen.findByText('Насос ГА-1')

    // The delete-row button is the "×" cell action in draft mode.
    const delButton = screen.getByRole('button', { name: '×' })
    await user.click(delButton)

    expect(confirm).toHaveBeenCalled()
    await waitFor(() => expect(periodPlanSvc.deleteItemFromPeriodPlan).toHaveBeenCalledWith(123, 501))
  })

  it('shows MRP snapshot action for a fixed plan and calls createMrpSnapshot', async () => {
    vi.mocked(periodPlanSvc.getPeriodPlanMatrix).mockResolvedValue(makeMatrix(fixedPlan))
    const user = userEvent.setup()
    renderAt('/period-plan/123')

    const snapshotBtn = await screen.findByRole('button', { name: 'MRP снимок' })
    await user.click(snapshotBtn)

    await waitFor(() => expect(periodPlanSvc.createMrpSnapshot).toHaveBeenCalledWith(123))
    expect(await screen.findByText(/MRP-снимок создан/)).toBeInTheDocument()
  })

  it('returns to the list via the "back" button (useNavigate)', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    await screen.findByText('Насос ГА-1')

    await user.click(screen.getByRole('button', { name: 'К списку планов' }))

    // List view chrome reappears after navigation.
    expect(await screen.findByRole('button', { name: 'Новый план' })).toBeInTheDocument()
    expect(screen.getByText('Планирование / Планирование выпуска')).toBeInTheDocument()
  })

  it('keeps Escape local to an input and navigates back from a neutral target', async () => {
    renderAt('/period-plan/123')
    const search = await screen.findByPlaceholderText(/поиск по мере ввода/)

    fireEvent.keyDown(search, { key: 'Escape' })
    expect(screen.getByRole('button', { name: 'К списку планов' })).toBeInTheDocument()

    const back = new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    })
    fireEvent(document, back)

    expect(await screen.findByRole('button', { name: 'Новый план' })).toBeInTheDocument()
    expect(back.defaultPrevented).toBe(true)
  })

  it('reloads only the active detail tab with F5 and refreshes runs', async () => {
    const user = userEvent.setup()
    renderAt('/period-plan/123')
    const search = await screen.findByPlaceholderText(/поиск по мере ввода/)
    await waitFor(() => expect(periodPlanSvc.listPeriodPlanRuns).toHaveBeenCalled())
    vi.mocked(periodPlanSvc.getPeriodPlanMatrix).mockClear()
    vi.mocked(periodPlanSvc.getExecutionJournal).mockClear()
    vi.mocked(periodPlanSvc.listPeriodPlanRuns).mockClear()

    const matrixRefresh = new KeyboardEvent('keydown', {
      key: 'F5',
      bubbles: true,
      cancelable: true,
    })
    fireEvent(search, matrixRefresh)

    await waitFor(() => {
      expect(periodPlanSvc.getPeriodPlanMatrix).toHaveBeenCalledTimes(1)
      expect(periodPlanSvc.listPeriodPlanRuns).toHaveBeenCalledTimes(1)
    })
    expect(periodPlanSvc.getExecutionJournal).not.toHaveBeenCalled()
    expect(matrixRefresh.defaultPrevented).toBe(true)

    await user.click(screen.getByRole('button', { name: 'Журнал исполнения' }))
    await waitFor(() => expect(periodPlanSvc.getExecutionJournal).toHaveBeenCalled())
    vi.mocked(periodPlanSvc.getPeriodPlanMatrix).mockClear()
    vi.mocked(periodPlanSvc.getExecutionJournal).mockClear()
    vi.mocked(periodPlanSvc.listPeriodPlanRuns).mockClear()

    fireEvent(document, new KeyboardEvent('keydown', {
      key: 'F5',
      bubbles: true,
      cancelable: true,
    }))

    await waitFor(() => {
      expect(periodPlanSvc.getExecutionJournal).toHaveBeenCalledTimes(1)
      expect(periodPlanSvc.listPeriodPlanRuns).toHaveBeenCalledTimes(1)
    })
    expect(periodPlanSvc.getPeriodPlanMatrix).not.toHaveBeenCalled()
  })
})
