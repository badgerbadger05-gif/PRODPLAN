import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../lib/api'
import { qty } from '../../lib/format'
import type {
  ItemLedgerDriftResponse,
  ItemLedgerMovementsResponse,
  ItemLedgerPosition,
  ItemLedgerReservationsResponse,
  ItemLedgerReservationEventsResponse,
} from '../../domain/itemLedger'
import {
  LedgerWorkspacePage,
  LedgerWorkspaceRoute,
  type ItemLedgerDataProvider,
} from './LedgerWorkspacePage'

type Defer<T> = {
  resolve: (value: T) => void
  promise: Promise<T>
}

function defer<T>(): Defer<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => {
    resolve = next
  })
  return { resolve, promise }
}

const position: ItemLedgerPosition = {
  item_id: 9401,
  item_code: '00000063',
  item_name: 'Труба',
  pool_key: '9401::default',
  on_hand: 335.144,
  on_hand_by_warehouse: [
    {
      warehouse_ref1c: 'MAIN',
      warehouse_name: 'Основной склад',
      qty: -3.12,
      qty_negative: true,
    },
    {
      warehouse_ref1c: 'SUP',
      warehouse_name: 'Склад поставок',
      qty: 12,
      qty_negative: false,
    },
  ],
  incoming_supplier: 120,
  incoming_wip: 0,
  incoming: 120,
  reserved_soft: 30,
  available: -5.25,
  projected: -71.06,
  uncovered: 71.06,
  flags: { on_hand_negative: true, has_uncovered: true, reconcile_pending: false },
}

const movements: ItemLedgerMovementsResponse = {
  total: 1,
  limit: 100,
  offset: 0,
  rows: [
    {
      id: 10,
      posting_at: '2026-07-21T10:00:00',
      warehouse_ref1c: 'MAIN',
      warehouse_name: 'Основной',
      qty: -18,
      qty_after: 100,
      movement_kind: 'assembly_out',
      record_type: 'Expense',
      recorder_type: 'Document_СборкаЗапасов',
      recorder_ref: 'doc-1',
      line_no: '2',
      ingest_source: 'document_pull',
      characteristic_ref: 'ХАР',
      organization_ref: 'org-1',
    },
    {
      id: 11,
      posting_at: '2026-07-22T10:00:00',
      warehouse_ref1c: 'MAIN',
      warehouse_name: 'Основной',
      qty: 6,
      qty_after: 106,
      movement_kind: 'receipt',
      record_type: 'Receipt',
      recorder_type: 'Document_Поступление',
      recorder_ref: 'doc-2',
      line_no: '',
      ingest_source: 'seed',
      characteristic_ref: 'ХАР',
      organization_ref: 'org-1',
    },
  ],
}

const reservations: ItemLedgerReservationsResponse = {
  rows: [
    {
      reservation_id: 101,
      run_id: 17,
      plan_id: 5,
      plan_name: 'АВГУСТ',
      requirement_id: 55831,
      realization_mode: 'consume',
      priority: { period_from: '2026-07-01', period_to: '2026-07-31' },
      reserved_qty: 270.64,
      realized_qty: 79.57,
      outstanding: 191.07,
      covered: { on_hand: 120, incoming_supplier: 71.07, incoming_wip: 0 },
      uncovered_qty: 0,
      lifecycle_status: 'active',
      coverage_state: 'covered',
    },
    {
      reservation_id: 102,
      run_id: 18,
      plan_id: 5,
      plan_name: 'СЕНТЯБРЬ',
      requirement_id: 56000,
      realization_mode: 'make',
      priority: { period_from: '2026-08-01', period_to: '2026-08-31' },
      reserved_qty: 80,
      realized_qty: 20,
      outstanding: 60,
      covered: { on_hand: 10, incoming_supplier: 20, incoming_wip: 5 },
      uncovered_qty: 30,
      lifecycle_status: 'released',
      coverage_state: 'partial',
    },
  ],
}

const drift: ItemLedgerDriftResponse = {
  total: 1,
  limit: 100,
  offset: 0,
  rows: [
    {
      id: 1,
      cycle_id: '',
      kind: 'shortfall',
      drift_qty: 3,
      expected_stock: 10,
      actual_stock: 7,
      at: '2026-07-21T11:00:00',
      cause: null,
      adjustment_sle_id: null,
      matured: false,
      first_seen_cycle_id: null,
      requirement_id: null,
      details: { note: 'supply gap', source: 'openapi' },
    },
  ],
}

const reservationEvents: ItemLedgerReservationEventsResponse = {
  reservation_id: 101,
  rows: [
    {
      id: 7,
      event_at: '2026-07-21T09:40:03',
      event_kind: 'realize',
      reserved_delta: 0,
      realized_delta: 79.57,
      sle_id: 88231,
      fact_ref: 'doc-1',
      fact_line_ref: 'line-2',
      match_rule: 'pegged',
      cycle_id: 'cycle-1',
    },
  ],
}

function createProvider(overrides: Partial<ItemLedgerDataProvider> = {}): ItemLedgerDataProvider {
  return {
    loadPosition: vi.fn().mockResolvedValue(position),
    loadMovements: vi.fn().mockResolvedValue(movements),
    loadReservations: vi.fn().mockResolvedValue(reservations),
    loadReservationEvents: vi.fn().mockResolvedValue(reservationEvents),
    loadDrift: vi.fn().mockResolvedValue(drift),
    ...overrides,
  }
}

function renderLedgerRoute(provider: ItemLedgerDataProvider) {
  return render(
    <MemoryRouter initialEntries={['/ledger']}>
      <Routes>
        <Route path="/ledger" element={<LedgerWorkspaceRoute provider={provider} />} />
        <Route path="/ledger/items/:itemId" element={<LedgerWorkspaceRoute provider={provider} />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('LedgerWorkspacePage', () => {
  it('opens an item from entry form and navigates to /ledger/items/:itemId', async () => {
    const provider = createProvider()
    renderLedgerRoute(provider)

    fireEvent.change(screen.getByLabelText('ID номенклатуры'), { target: { value: '9401' } })
    fireEvent.click(screen.getByRole('button', { name: 'Открыть' }))

    expect(await screen.findByText('Труба')).toBeInTheDocument()
    expect(provider.loadPosition).toHaveBeenCalledWith(9401, expect.any(AbortSignal))
  })

  it('shows contract data and preserves negative values', async () => {
    render(<LedgerWorkspacePage itemId="9401" provider={createProvider()} />)

    expect(await screen.findByText('Труба')).toBeInTheDocument()
    expect(screen.getAllByText(qty(-3.12)).length).toBeGreaterThan(0)
    expect(screen.getByText(qty(-5.25))).toBeInTheDocument()
  })

  it('loads reservation events after row selection', async () => {
    render(<LedgerWorkspacePage itemId="9401" provider={createProvider()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Резервы' }))
    const row = await screen.findByRole('row', { name: /Резерв 101/ })
    fireEvent.click(row)

    expect(await screen.findByText(/Погашен/)).toBeInTheDocument()
    expect(screen.getByText(/SLE #88231/)).toBeInTheDocument()
    expect(screen.getByText(/doc-1/)).toBeInTheDocument()
  })

  it('renders 404 contract error state', async () => {
    const provider = createProvider({
      loadPosition: vi.fn().mockRejectedValue(new ApiError('not found', 404, null)),
    })

    render(<LedgerWorkspacePage itemId="9999" provider={provider} />)

    expect(await screen.findByText('Номенклатура не найдена')).toBeInTheDocument()
  })

  it('keeps latest reservation events when selections change quickly', async () => {
    const firstEvents = defer<ItemLedgerReservationEventsResponse>()
    const secondEvents = defer<ItemLedgerReservationEventsResponse>()
    const provider: ItemLedgerDataProvider = {
      loadPosition: vi.fn().mockResolvedValue(position),
      loadMovements: vi.fn().mockResolvedValue(movements),
      loadReservations: vi.fn().mockResolvedValue(reservations),
      loadDrift: vi.fn().mockResolvedValue(drift),
      loadReservationEvents: vi.fn((_, reservationId) => {
        if (reservationId === 101) return firstEvents.promise
        return secondEvents.promise
      }),
    }

    render(<LedgerWorkspacePage itemId="9401" provider={provider} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Резервы' }))
    fireEvent.click(await screen.findByRole('row', { name: /Резерв 101/ }))
    fireEvent.click(screen.getByRole('row', { name: /Резерв 102/ }))

    secondEvents.resolve({
      reservation_id: 102,
      rows: [
        {
          id: 8,
          event_at: '2026-07-21T10:00:00',
          event_kind: 'open',
          reserved_delta: 20,
          realized_delta: 0,
          sle_id: null,
          fact_ref: 'doc-2',
          fact_line_ref: '',
          match_rule: 'manual',
          cycle_id: 'cycle-2',
        },
      ],
    })

    firstEvents.resolve({
      reservation_id: 101,
      rows: [
        {
          id: 7,
          event_at: '2026-07-21T09:40:03',
          event_kind: 'realize',
          reserved_delta: 0,
          realized_delta: 79.57,
          sle_id: 88231,
          fact_ref: 'doc-1',
          fact_line_ref: 'line-2',
          match_rule: 'pegged',
          cycle_id: 'cycle-1',
        },
      ],
    })

    await waitFor(() => {
      expect(screen.getByText(/doc-2/)).toBeInTheDocument()
      expect(screen.queryByText(/doc-1/)).not.toBeInTheDocument()
    })
  })
})
