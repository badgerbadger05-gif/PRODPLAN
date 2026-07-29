import type { components } from '../lib/apiTypes'

type ApiSchemas = components['schemas']

export type ItemLedgerPosition = ApiSchemas['ItemLedgerPositionResponse']
export type ItemLedgerMovementRow = ApiSchemas['ItemLedgerMovement']
export type ItemLedgerMovementsResponse = ApiSchemas['ItemLedgerMovementsResponse']
export type ItemLedgerReservationRow = ApiSchemas['ItemLedgerReservationRow']
export type ItemLedgerReservationsResponse = ApiSchemas['ItemLedgerReservationsResponse']
export type ItemLedgerReservationEventRow = ApiSchemas['ItemLedgerReservationEventRow']
export type ItemLedgerReservationEventsResponse = ApiSchemas['ItemLedgerReservationEventsResponse']
