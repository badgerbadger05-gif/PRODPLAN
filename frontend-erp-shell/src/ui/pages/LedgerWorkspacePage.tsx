import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  getItemLedgerDrift,
  getItemLedgerMovements,
  getItemLedgerPosition,
  getItemLedgerReservationEvents,
  getItemLedgerReservations,
  type ItemLedgerDriftPagination,
  type ItemLedgerMovementsFilters,
  type ItemLedgerReservationsFilters,
} from '../../services/itemLedger'
import type {
  ItemLedgerDriftResponse,
  ItemLedgerMovementsResponse,
  ItemLedgerPosition,
  ItemLedgerReservationsResponse,
  ItemLedgerReservationEventsResponse,
} from '../../domain/itemLedger'
import {
  ItemLedgerDriftTable,
  ItemLedgerMovementsTable,
  ItemLedgerPositionSummary,
  ItemLedgerReservationEventsTimeline,
  ItemLedgerReservationsTable,
} from '../item-ledger'
import { DocumentWindow } from '../layout/DocumentWindow'
import { Button } from '../kit'

type Tab = 'movements' | 'reservations' | 'drift'

type MovementFilterState = {
  date_from: string
  date_to: string
  warehouse_ref1c: string
}

type ReservationFilterState = {
  status: string
  run_id: string
}

export interface ItemLedgerDataProvider {
  loadPosition(itemId: number, signal?: AbortSignal): Promise<ItemLedgerPosition>
  loadMovements(
    itemId: number,
    filters: ItemLedgerMovementsFilters,
    signal?: AbortSignal,
  ): Promise<ItemLedgerMovementsResponse>
  loadReservations(
    itemId: number,
    filters: ItemLedgerReservationsFilters,
    signal?: AbortSignal,
  ): Promise<ItemLedgerReservationsResponse>
  loadReservationEvents(
    itemId: number,
    reservationId: number,
    signal?: AbortSignal,
  ): Promise<ItemLedgerReservationEventsResponse>
  loadDrift(
    itemId: number,
    pagination?: ItemLedgerDriftPagination,
    signal?: AbortSignal,
  ): Promise<ItemLedgerDriftResponse>
}

const defaultItemLedgerProvider: ItemLedgerDataProvider = {
  loadPosition: (itemId, signal) => getItemLedgerPosition(itemId, signal),
  loadMovements: (itemId, filters, signal) => getItemLedgerMovements(itemId, filters, signal),
  loadReservations: (itemId, filters, signal) => getItemLedgerReservations(itemId, filters, signal),
  loadReservationEvents: (itemId, reservationId, signal) => getItemLedgerReservationEvents(itemId, reservationId, signal),
  loadDrift: (itemId, pagination, signal) => getItemLedgerDrift(itemId, pagination, signal),
}

type Props = {
  provider?: ItemLedgerDataProvider
  itemId?: string | null
  onOpenItem?: (itemId: number) => void
}

type LoadingState = {
  movements: boolean
  reservations: boolean
  drift: boolean
}

type ErrorState = {
  position: string
  movements: string
  reservations: string
  drift: string
  reservationEvents: string
}

const defaultMovementFilters: MovementFilterState = {
  date_from: '',
  date_to: '',
  warehouse_ref1c: '',
}

const defaultReservationFilters: ReservationFilterState = {
  status: '',
  run_id: '',
}

const reservationStatusOptions = ['active', 'closed', 'released', 'carried', 'cancelled']

function toNumber(value: string) {
  if (!value.trim()) return null
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

function readableError(error: unknown) {
  if (typeof error === 'object' && error !== null && 'status' in error && error.status === 404) {
    return 'Номенклатура не найдена'
  }
  if (error instanceof Error) return error.message
  return String(error)
}

function normalizeMovementFilters(filters: MovementFilterState): ItemLedgerMovementsFilters {
  return {
    date_from: filters.date_from || null,
    date_to: filters.date_to || null,
    warehouse_ref1c: filters.warehouse_ref1c || null,
  }
}

function normalizeReservationFilters(filters: ReservationFilterState): ItemLedgerReservationsFilters {
  return {
    status: filters.status || null,
    run_id: toNumber(filters.run_id),
  }
}

function toItemId(raw: string | null | undefined) {
  if (!raw) return null
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

export function LedgerWorkspacePage({
  provider = defaultItemLedgerProvider,
  itemId = null,
  onOpenItem,
}: Props) {
  const resolvedItemId = toItemId(itemId)
  const hasRouteItemId = resolvedItemId !== null

  const [tab, setTab] = useState<Tab>('movements')
  const [position, setPosition] = useState<ItemLedgerPosition | null>(null)
  const [movements, setMovements] = useState<ItemLedgerMovementsResponse['rows']>([])
  const [reservations, setReservations] = useState<ItemLedgerReservationsResponse['rows']>([])
  const [drift, setDrift] = useState<ItemLedgerDriftResponse['rows']>([])

  const [entryItemId, setEntryItemId] = useState('')
  const [entryError, setEntryError] = useState('')

  const [movementDraftFilters, setMovementDraftFilters] = useState<MovementFilterState>(defaultMovementFilters)
  const [movementFilters, setMovementFilters] = useState<MovementFilterState>(defaultMovementFilters)
  const [reservationDraftFilters, setReservationDraftFilters] = useState<ReservationFilterState>(defaultReservationFilters)
  const [reservationFilters, setReservationFilters] = useState<ReservationFilterState>(defaultReservationFilters)

  const [selectedReservationId, setSelectedReservationId] = useState<number | null>(null)
  const [reservationEvents, setReservationEvents] = useState<ItemLedgerReservationEventsResponse['rows']>([])
  const [reservationEventsLoading, setReservationEventsLoading] = useState(false)

  const [loadingState, setLoadingState] = useState<LoadingState>({
    movements: true,
    reservations: true,
    drift: true,
  })
  const [errors, setErrors] = useState<ErrorState>({
    position: '',
    movements: '',
    reservations: '',
    drift: '',
    reservationEvents: '',
  })

  const loadSequence = useRef(0)
  const eventsSequence = useRef(0)

  useEffect(() => {
    if (!hasRouteItemId || itemId === null) {
      setPosition(null)
      setMovements([])
      setReservations([])
      setDrift([])
      setSelectedReservationId(null)
      setReservationEvents([])
      setErrors({ position: '', movements: '', reservations: '', drift: '', reservationEvents: '' })
      setLoadingState({ movements: false, reservations: false, drift: false })
      return
    }

    const controller = new AbortController()
    const sequence = ++loadSequence.current

    setLoadingState({ movements: true, reservations: true, drift: true })
    setErrors({ position: '', movements: '', reservations: '', drift: '', reservationEvents: '' })
    setReservationEvents([])
    setSelectedReservationId(null)

    void Promise.allSettled([
      provider.loadPosition(resolvedItemId, controller.signal),
      provider.loadMovements(resolvedItemId, normalizeMovementFilters(movementFilters), controller.signal),
      provider.loadReservations(resolvedItemId, normalizeReservationFilters(reservationFilters), controller.signal),
      provider.loadDrift(resolvedItemId, {}, controller.signal),
    ]).then(([positionResult, movementsResult, reservationsResult, driftResult]) => {
      if (sequence !== loadSequence.current || controller.signal.aborted) return

      if (positionResult.status === 'fulfilled') {
        setPosition(positionResult.value)
      } else {
        setPosition(null)
        setErrors((state) => ({ ...state, position: readableError(positionResult.reason) }))
      }

      if (movementsResult.status === 'fulfilled') {
        setMovements(movementsResult.value.rows)
      } else {
        setMovements([])
        setErrors((state) => ({ ...state, movements: readableError(movementsResult.reason) }))
      }

      if (reservationsResult.status === 'fulfilled') {
        setReservations(reservationsResult.value.rows)
      } else {
        setReservations([])
        setErrors((state) => ({ ...state, reservations: readableError(reservationsResult.reason) }))
      }

      if (driftResult.status === 'fulfilled') {
        setDrift(driftResult.value.rows)
      } else {
        setDrift([])
        setErrors((state) => ({ ...state, drift: readableError(driftResult.reason) }))
      }

      setLoadingState({ movements: false, reservations: false, drift: false })
    })

    return () => controller.abort()
  }, [itemId, hasRouteItemId, provider, movementFilters, reservationFilters, resolvedItemId])

  useEffect(() => {
    if (!hasRouteItemId || selectedReservationId === null) {
      setReservationEvents([])
      setReservationEventsLoading(false)
      setErrors((state) => ({ ...state, reservationEvents: '' }))
      return
    }

    const controller = new AbortController()
    const sequence = ++eventsSequence.current
    setReservationEventsLoading(true)
    setErrors((state) => ({ ...state, reservationEvents: '' }))

    provider
      .loadReservationEvents(resolvedItemId, selectedReservationId, controller.signal)
      .then((next) => {
        if (sequence !== eventsSequence.current || controller.signal.aborted) return
        setReservationEvents(next.rows)
      })
      .catch((reason) => {
        if (sequence !== eventsSequence.current || controller.signal.aborted) return
        setReservationEvents([])
        setErrors((state) => ({ ...state, reservationEvents: readableError(reason) }))
      })
      .finally(() => {
        if (sequence !== eventsSequence.current || controller.signal.aborted) return
        setReservationEventsLoading(false)
      })

    return () => controller.abort()
  }, [hasRouteItemId, resolvedItemId, selectedReservationId, provider])

  const openItem = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const parsed = toItemId(entryItemId)
    if (parsed === null) {
      setEntryError('ID номенклатуры должен быть положительным числом')
      return
    }
    setEntryError('')
    onOpenItem?.(parsed)
  }

  if (!itemId) {
    return (
      <main className="workArea">
        <div className="topLine">
          <div className="breadcrumbs">Ledger / Позиция номенклатуры</div>
          <div className="runBadge">чтение · только просмотр</div>
        </div>
        <DocumentWindow
          title="Ledger по номенклатуре"
          subtitle="Введите id номенклатуры для просмотра движений, резервов и дрейфа"
          footer={<div className="ledgerStatus">Только чтение</div>}
        >
          <form className="ledgerFilters" onSubmit={openItem}>
            <label>
              ID номенклатуры
              <input
                aria-label="ID номенклатуры"
                value={entryItemId}
                onChange={(event) => {
                  setEntryItemId(event.target.value)
                  if (entryError) setEntryError('')
                }}
              />
            </label>
            <Button type="submit">Открыть</Button>
          </form>
          {entryError && <div role="alert" className="errorLine">{entryError}</div>}
        </DocumentWindow>
      </main>
    )
  }

  if (!hasRouteItemId) {
    return (
      <main className="workArea">
        <div className="topLine">
          <div className="breadcrumbs">Ledger / Позиция номенклатуры</div>
        </div>
        <DocumentWindow
          title="Ledger по номенклатуре"
          subtitle="Неверный идентификатор номенклатуры"
          footer={<div className="ledgerStatus">Только чтение</div>}
        >
          <div role="alert" className="errorLine">ID номенклатуры должен быть положительным числом</div>
        </DocumentWindow>
      </main>
    )
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Ledger / Позиция номенклатуры</div>
        <div className="runBadge">чтение · только просмотр</div>
      </div>
      <DocumentWindow
        title={`Ledger: ${position?.item_code ?? itemId}`}
        subtitle="Наблюдение: движения, резервы, дрейф"
        hotkeys="Enter Применить"
        footer={(
          <div className="ledgerStatus" aria-live="polite">
            {position ? `Номенклатура: ${position.item_name} · ID ${position.item_id}` : 'Загрузка номенклатуры...'}
          </div>
        )}
      >
        <div className="toolbar ledgerToolbar">
          <Button variant={tab === 'movements' ? 'primary' : 'default'} onClick={() => setTab('movements')}>Движения</Button>
          <Button variant={tab === 'reservations' ? 'primary' : 'default'} onClick={() => setTab('reservations')}>Резервы</Button>
          <Button variant={tab === 'drift' ? 'primary' : 'default'} onClick={() => setTab('drift')}>Дрейф</Button>
          <span className="toolbarSpacer" />
          <Button
            onClick={() => {
              setMovementFilters({ ...movementDraftFilters })
              setReservationFilters({ ...reservationDraftFilters })
            }}
          >
            Обновить
          </Button>
        </div>

        {errors.position && (
          <div role="alert" className="errorLine">
            {errors.position}
          </div>
        )}

        {position ? <ItemLedgerPositionSummary position={position} /> : !errors.position ? <div className="emptyDetail">Загрузка позиции...</div> : null}

        {tab === 'movements' && (
          <>
            <form
              className="ledgerFilters"
              onSubmit={(event) => {
                event.preventDefault()
                setMovementFilters({ ...movementDraftFilters })
              }}
            >
              <label>
                Дата с
                <input
                  aria-label="date_from"
                  type="date"
                  value={movementDraftFilters.date_from}
                  onChange={(event) => setMovementDraftFilters({ ...movementDraftFilters, date_from: event.target.value })}
                />
              </label>
              <label>
                Дата по
                <input
                  aria-label="date_to"
                  type="date"
                  value={movementDraftFilters.date_to}
                  onChange={(event) => setMovementDraftFilters({ ...movementDraftFilters, date_to: event.target.value })}
                />
              </label>
              <label>
                Склад
                <input
                  aria-label="warehouse"
                  value={movementDraftFilters.warehouse_ref1c}
                  onChange={(event) => setMovementDraftFilters({
                    ...movementDraftFilters,
                    warehouse_ref1c: event.target.value,
                  })}
                />
              </label>
              <Button type="submit">Применить</Button>
            </form>
            {errors.movements && <div role="alert" className="errorLine">{errors.movements}</div>}
            {loadingState.movements ? <div>Загрузка движений...</div> : <ItemLedgerMovementsTable rows={movements} />}
          </>
        )}

        {tab === 'reservations' && (
          <>
            <form
              className="ledgerFilters"
              onSubmit={(event) => {
                event.preventDefault()
                setReservationFilters({ ...reservationDraftFilters })
              }}
            >
              <label>
                Статус
                <select
                  aria-label="status"
                  value={reservationDraftFilters.status}
                  onChange={(event) => setReservationDraftFilters({
                    ...reservationDraftFilters,
                    status: event.target.value,
                  })}
                >
                  <option value="">Все</option>
                  {reservationStatusOptions.map((status) => (
                    <option value={status} key={status}>{status}</option>
                  ))}
                </select>
              </label>
              <label>
                Run ID
                <input
                  aria-label="run_id"
                  value={reservationDraftFilters.run_id}
                  onChange={(event) => setReservationDraftFilters({
                    ...reservationDraftFilters,
                    run_id: event.target.value,
                  })}
                />
              </label>
              <Button type="submit">Применить</Button>
            </form>
            {errors.reservations && <div role="alert" className="errorLine">{errors.reservations}</div>}
            {loadingState.reservations
              ? (
                <div>Загрузка резервов...</div>
              )
              : (
                <div className="ledgerSplit">
                  <div className="tablePane">
                    <ItemLedgerReservationsTable
                      rows={reservations}
                      selectedReservationId={selectedReservationId}
                      onSelect={(row) => {
                        setSelectedReservationId(row.reservation_id)
                      }}
                    />
                  </div>
                  <aside className="detailPane">
                    {!selectedReservationId && <div className="emptyDetail">Выберите резерв для журнала событий</div>}
                    {selectedReservationId && (
                      <>
                        <h3>События резерва #{selectedReservationId}</h3>
                        {errors.reservationEvents && <div role="alert" className="errorLine">{errors.reservationEvents}</div>}
                        {reservationEventsLoading
                          ? <div>Загрузка событий резерва...</div>
                          : <ItemLedgerReservationEventsTimeline rows={reservationEvents} />}
                      </>
                    )}
                  </aside>
                </div>
              )}
          </>
        )}

        {tab === 'drift' && (
          <>
            {errors.drift && <div role="alert" className="errorLine">{errors.drift}</div>}
            {loadingState.drift ? <div>Загрузка дрейфа...</div> : <ItemLedgerDriftTable rows={drift} />}
          </>
        )}
      </DocumentWindow>
    </main>
  )
}

export function LedgerWorkspaceRoute({ provider }: { provider?: ItemLedgerDataProvider }) {
  const { itemId } = useParams()
  const navigate = useNavigate()
  return (
    <LedgerWorkspacePage
      provider={provider}
      itemId={itemId}
      onOpenItem={(nextItemId) => navigate(`/ledger/items/${encodeURIComponent(String(nextItemId))}`)}
    />
  )
}
