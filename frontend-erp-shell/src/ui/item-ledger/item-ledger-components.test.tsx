import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  ItemLedgerDriftTable,
  ItemLedgerMovementsTable,
  ItemLedgerPositionSummary,
  ItemLedgerReservationEventsTimeline,
  ItemLedgerReservationsTable,
} from './index'
import { qty } from '../../lib/format'
import type { ItemLedgerDriftResponse, ItemLedgerMovementRow, ItemLedgerPosition, ItemLedgerReservationsResponse, ItemLedgerReservationEventsResponse } from '../../domain/itemLedger'

const truthMeta = {
  ledger_generation: 4,
  cutoff: '2026-07-23T12:00:00',
  truth_status: 'accepted',
  truth_reason: null,
}

const position: ItemLedgerPosition = {
  truth_meta: truthMeta,
  item_id: 9401,
  item_code: '00000063',
  item_name: 'Труба',
  pool_key: '9401::default',
  on_hand: 120.5,
  on_hand_by_warehouse: [
    {
      warehouse_ref1c: 'MAIN',
      warehouse_name: 'Основной склад',
      qty: -3.12,
      qty_negative: true,
    },
    {
      warehouse_ref1c: 'SUP',
      warehouse_name: 'Поставки',
      qty: 15,
      qty_negative: false,
    },
  ],
  incoming_supplier: 120,
  incoming_wip: 0,
  incoming: 120,
  reserved_soft: 2.5,
  available: -5.25,
  projected: -71.06,
  uncovered: 71.06,
  flags: {
    on_hand_negative: true,
    has_uncovered: true,
    reconcile_pending: false,
  },
}

const movements: ItemLedgerMovementRow[] = [
  {
    id: 1,
    posting_at: '2026-07-21T09:40:03',
    warehouse_ref1c: 'MAIN',
    warehouse_name: 'Основной',
    qty: -40,
    qty_after: 295.144,
    movement_kind: 'assembly_out',
    record_type: 'Expense',
    recorder_type: 'Document_СборкаЗапасов',
    recorder_ref: 'r-1',
    line_no: '2',
    ingest_source: 'document_pull',
    characteristic_ref: '',
    organization_ref: 'org-1',
  },
]

const reservations: ItemLedgerReservationsResponse['rows'] = [
  {
    reservation_id: 101,
    run_id: 17,
    plan_id: 5,
    plan_name: 'АВГУСТ',
    requirement_id: 55831,
    realization_mode: 'consume',
    priority: { period_from: '2026-08-01', period_to: '2026-08-31' },
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
    run_id: 17,
    plan_id: 5,
    plan_name: 'СРЕДА',
    requirement_id: 56000,
    realization_mode: 'make',
    priority: { period_from: '2026-09-01', period_to: '2026-09-30' },
    reserved_qty: 80,
    realized_qty: 20,
    outstanding: 60,
    covered: { on_hand: 10, incoming_supplier: 20, incoming_wip: 5 },
    uncovered_qty: 30,
    lifecycle_status: 'released',
    coverage_state: 'partial',
  },
]

const events: ItemLedgerReservationEventsResponse['rows'] = [
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
]

const driftRows: ItemLedgerDriftResponse['rows'] = [
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
]

describe('item-ledger presentation primitives', () => {
  it('shows position summary with warehouse breakdown and negative values', () => {
    render(<ItemLedgerPositionSummary position={position} />)
    expect(screen.getByText('Сводка позиции')).toBeInTheDocument()
    expect(screen.getByText('00000063')).toBeInTheDocument()
    expect(screen.getByText(qty(-3.12))).toBeInTheDocument()
    expect(screen.getByText(qty(-5.25))).toBeInTheDocument()
    expect(screen.getByText('Основной склад')).toBeInTheDocument()
  })

  it('renders movement rows and keeps negative movement values visible', () => {
    render(<ItemLedgerMovementsTable rows={movements} />)
    expect(screen.getByRole('table', { name: 'Движения номенклатуры' })).toBeInTheDocument()
    expect(screen.getByText(qty(-40))).toBeInTheDocument()
  })

  it('renders reservations table, labels modes, and returns selected reservation', () => {
    const onSelect = vi.fn()
    render(<ItemLedgerReservationsTable rows={reservations} onSelect={onSelect} />)
    expect(screen.getByText('Списание (consume)')).toBeInTheDocument()
    expect(screen.getByText('Выпуск (make)')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /Резерв 101/ })
    fireEvent.click(row)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ reservation_id: 101 }))
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledTimes(2)
  })

  it('renders reservation events with SLE and fact linkage', () => {
    render(<ItemLedgerReservationEventsTimeline rows={events} />)
    expect(screen.getByText(/Погашен/)).toBeInTheDocument()
    expect(screen.getByText(/SLE #88231/)).toBeInTheDocument()
    expect(screen.getByText(/doc-1/)).toBeInTheDocument()
  })

  it('renders drift rows with nullable fields and extra debug details', () => {
    render(<ItemLedgerDriftTable rows={driftRows} />)
    expect(screen.getByRole('table', { name: 'Таблица дрейфа номенклатуры' })).toBeInTheDocument()
    expect(screen.getByText(/\+3/)).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/"note"/)).toBeInTheDocument()
  })
})
