import { api } from '../lib/api'
import type { paths } from '../lib/apiTypes'
import type {
  ItemLedgerMovementsResponse,
  ItemLedgerFutureSupplyResponse,
  ItemLedgerPosition,
  ItemLedgerReservationsResponse,
  ItemLedgerReservationEventsResponse,
} from '../domain/itemLedger'

const ITEM_LEDGER_BASE = '/v1/item-ledger'

export type ItemLedgerMovementsFilters = NonNullable<
  paths['/api/v1/item-ledger/{item_id}/movements']['get']['parameters']['query']
>
export type ItemLedgerReservationsFilters = NonNullable<
  paths['/api/v1/item-ledger/{item_id}/reservations']['get']['parameters']['query']
>

function toQuery(params: Record<string, string | number | undefined | null>) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    query.set(key, String(value))
  })
  return query.toString()
}

export function getItemLedgerPosition(itemId: number, signal?: AbortSignal) {
  return api<ItemLedgerPosition>(`${ITEM_LEDGER_BASE}/${encodeURIComponent(itemId)}/position`, undefined, signal)
}

export function getItemLedgerFutureSupply(itemId: number, signal?: AbortSignal) {
  return api<ItemLedgerFutureSupplyResponse>(
    `${ITEM_LEDGER_BASE}/${encodeURIComponent(itemId)}/future-supply`,
    undefined,
    signal,
  )
}

export function getItemLedgerMovements(itemId: number, filters: ItemLedgerMovementsFilters = {}, signal?: AbortSignal) {
  const query = toQuery({
    date_from: filters.date_from,
    date_to: filters.date_to,
    warehouse_ref1c: filters.warehouse_ref1c,
    limit: filters.limit,
    offset: filters.offset,
  })
  return api<ItemLedgerMovementsResponse>(
    `${ITEM_LEDGER_BASE}/${encodeURIComponent(itemId)}/movements${query ? `?${query}` : ''}`,
    undefined,
    signal,
  )
}

export function getItemLedgerReservations(itemId: number, filters: ItemLedgerReservationsFilters = {}, signal?: AbortSignal) {
  const query = toQuery({
    status: filters.status,
    run_id: filters.run_id,
  })
  return api<ItemLedgerReservationsResponse>(
    `${ITEM_LEDGER_BASE}/${encodeURIComponent(itemId)}/reservations${query ? `?${query}` : ''}`,
    undefined,
    signal,
  )
}

export function getItemLedgerReservationEvents(itemId: number, reservationId: number, signal?: AbortSignal) {
  return api<ItemLedgerReservationEventsResponse>(
    `${ITEM_LEDGER_BASE}/${encodeURIComponent(itemId)}/reservations/${encodeURIComponent(
      reservationId,
    )}/events`,
    undefined,
    signal,
  )
}
