import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ActionContext,
  DialogRequest,
  Doctype,
  RowId,
  SortState,
} from './types'
import { canRunAction, canView, canViewRecord, type AccessSubject } from './permissions'
import type { ViewState } from '../views'

const DEFAULT_LIMIT = 100

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

export function useDoctypeList<Row, Filters extends object, Detail>(
  doctype: Doctype<Row, Filters, Detail>,
  options: { limit?: number; access: AccessSubject },
) {
  const limit = options.limit ?? DEFAULT_LIMIT
  const enabled = canView(doctype.permissions, options.access)
  const accessKey = JSON.stringify({
    roles: [...options.access.roles].sort(),
    permissions: [...options.access.permissions].sort(),
  })
  const [rows, setRows] = useState<Row[]>([])
  const [filters, setFilters] = useState<Filters>(doctype.initialFilters)
  const [appliedFilters, setAppliedFilters] = useState<Filters>(doctype.initialFilters)
  const [sort, setSortState] = useState<SortState | null>(null)
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [listMeta, setListMeta] = useState<Record<string, unknown>>({})
  const [activeId, setActiveId] = useState<RowId | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<RowId>>(() => new Set())
  const [detail, setDetail] = useState<Detail | null>(null)
  const [listLoading, setListLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [dialog, setDialog] = useState<DialogRequest | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const loadSequence = useRef(0)
  const detailSequence = useRef(0)
  const actionInFlight = useRef(false)
  const mounted = useRef(true)
  const filterTimers = useRef<Map<keyof Filters, ReturnType<typeof setTimeout>>>(new Map())

  useEffect(() => () => {
    mounted.current = false
    filterTimers.current.forEach(clearTimeout)
  }, [])

  const rowId = useCallback(
    (row: Row) => (row[doctype.meta.idField] as RowId),
    [doctype.meta.idField],
  )

  const activeRow = useMemo(
    () => rows.find((row) => rowId(row) === activeId) ?? rows[0] ?? null,
    [activeId, rowId, rows],
  )
  const selection = useMemo(
    () => rows.filter((row) => selectedIds.has(rowId(row))),
    [rowId, rows, selectedIds],
  )
  const actionContext = useMemo<ActionContext<Row>>(
    () => ({ rows, activeRow, selection }),
    [activeRow, rows, selection],
  )

  useEffect(() => {
    if (!enabled) {
      setRows([])
      setTotal(0)
      setListMeta({})
      setActiveId(null)
      setSelectedIds(new Set())
      setDetail(null)
      return
    }
    const controller = new AbortController()
    const sequence = ++loadSequence.current
    const access = JSON.parse(accessKey) as AccessSubject
    setListLoading(true)
    setError('')
    setListMeta({})

    void doctype.dataSource
      .list(
        {
          limit,
          offset,
          filters: appliedFilters,
          sortBy: sort?.sortBy,
          sortDir: sort?.sortDir,
        },
        controller.signal,
      )
      .then((result) => {
        if (sequence !== loadSequence.current) return
        const resultRows = result.rows ?? []
        const permittedRows = resultRows.filter((row) => canViewRecord(doctype.permissions, row, access))
        setRows(permittedRows)
        setTotal(Math.max(0, (result.total ?? 0) - (resultRows.length - permittedRows.length)))
        const {
          rows: _rows,
          total: _total,
          limit: _limit,
          offset: _offset,
          ...meta
        } = result
        void _rows
        void _total
        void _limit
        void _offset
        setListMeta(meta)
        setSelectedIds((current) => {
          const visible = new Set(permittedRows.map(rowId))
          return new Set([...current].filter((id) => visible.has(id)))
        })
        setActiveId((current) => {
          if (current != null && permittedRows.some((row) => rowId(row) === current)) return current
          return permittedRows[0] ? rowId(permittedRows[0]) : null
        })
      })
      .catch((loadError: unknown) => {
        if (!controller.signal.aborted && sequence === loadSequence.current) {
          setError(errorMessage(loadError))
        }
      })
      .finally(() => {
        if (sequence === loadSequence.current) setListLoading(false)
      })

    return () => controller.abort()
  }, [accessKey, appliedFilters, doctype.dataSource, doctype.permissions, enabled, limit, offset, reloadKey, rowId, sort])

  useEffect(() => {
    if (!activeRow || !doctype.dataSource.detail) {
      setDetail(null)
      return
    }

    const controller = new AbortController()
    const sequence = ++detailSequence.current
    setDetail(null)
    setDetailLoading(true)
    setError('')
    void doctype.dataSource
      .detail(rowId(activeRow), controller.signal)
      .then((result) => {
        if (!controller.signal.aborted && sequence === detailSequence.current) setDetail(result)
      })
      .catch((detailError: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(detailError))
      })
      .finally(() => {
        if (!controller.signal.aborted && sequence === detailSequence.current) setDetailLoading(false)
      })
    return () => controller.abort()
  }, [activeRow, doctype.dataSource, rowId])

  const setFilter = useCallback(<Key extends keyof Filters>(key: Key, value: Filters[Key]) => {
    setFilters((current) => ({ ...current, [key]: value }))
    setOffset(0)
    const definition = doctype.filters?.find(
      (filter) => filter.kind !== 'dateRange'
        ? filter.field === key
        : filter.fieldFrom === key || filter.fieldTo === key,
    )
    if (definition?.kind === 'search' && definition.mode === 'submit') return
    const debounceMs = definition?.kind === 'search' ? (definition.debounceMs ?? 300) : 0
    const currentTimer = filterTimers.current.get(key)
    if (currentTimer) clearTimeout(currentTimer)
    if (!debounceMs) {
      setAppliedFilters((current) => ({ ...current, [key]: value }))
      return
    }
    filterTimers.current.set(key, setTimeout(() => {
      setAppliedFilters((current) => ({ ...current, [key]: value }))
      filterTimers.current.delete(key)
    }, debounceMs))
  }, [doctype.filters])

  const applyFilters = useCallback(() => {
    filterTimers.current.forEach(clearTimeout)
    filterTimers.current.clear()
    setOffset(0)
    setAppliedFilters(filters)
  }, [filters])

  const setSort = useCallback((sortBy: string) => {
    setSortState((current) => ({
      sortBy,
      sortDir: current?.sortBy === sortBy && current.sortDir === 'asc' ? 'desc' : 'asc',
    }))
    setOffset(0)
  }, [])

  const applyViewState = useCallback((view: Pick<ViewState, 'filters' | 'sort'>) => {
    filterTimers.current.forEach(clearTimeout)
    filterTimers.current.clear()
    const nextFilters = { ...doctype.initialFilters, ...view.filters } as Filters
    setFilters(nextFilters)
    setAppliedFilters(nextFilters)
    const nextSort = view.sort[0]
    setSortState(nextSort ? { sortBy: nextSort.field, sortDir: nextSort.direction } : null)
    setOffset(0)
  }, [doctype.initialFilters])

  const toggleSelection = useCallback((id: RowId) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setActiveId(id)
  }, [])

  const reload = useCallback(() => setReloadKey((current) => current + 1), [])

  const runAction = useCallback(async (key: string) => {
    const action = doctype.actions?.find((candidate) => candidate.key === key)
    if (
      !action
      || actionInFlight.current
      || !canRunAction(doctype.permissions, key, options.access)
      || action.enabled?.(actionContext) === false
      || (action.scope === 'selection' && actionContext.selection.length === 0)
      || (action.scope === 'row' && !actionContext.activeRow)
    ) return
    const confirmation = typeof action.confirm === 'function'
      ? action.confirm(actionContext)
      : action.confirm
    if (confirmation && !window.confirm(confirmation)) return

    actionInFlight.current = true
    setActionLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await action.run(actionContext)
      if (!mounted.current) return
      setMessage(result.message ?? '')
      setError(result.error ?? '')
      setDialog(result.open ?? null)
      if (result.reload) reload()
    } catch (actionError) {
      if (mounted.current) setError(errorMessage(actionError))
    } finally {
      actionInFlight.current = false
      if (mounted.current) setActionLoading(false)
    }
  }, [actionContext, doctype.actions, doctype.permissions, options.access, reload])

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return {
    rows,
    activeRow,
    activeId,
    setActiveId,
    detail,
    listMeta,
    filters,
    setFilter,
    applyFilters,
    sort,
    setSort,
    applyViewState,
    selection,
    selectedIds,
    toggleSelection,
    paging: {
      limit,
      offset,
      total,
      visibleFrom,
      visibleTo,
      canPrev: offset > 0,
      canNext: offset + rows.length < total,
      prev: () => setOffset((current) => Math.max(0, current - limit)),
      next: () => setOffset((current) => current + limit),
    },
    loading: listLoading || actionLoading,
    listLoading,
    actionLoading,
    detailLoading,
    error,
    message,
    dialog,
    closeDialog: () => setDialog(null),
    actionContext,
    runAction,
    reload,
  }
}

export type DoctypeListState<Row, Filters extends object, Detail> = ReturnType<
  typeof useDoctypeList<Row, Filters, Detail>
>
