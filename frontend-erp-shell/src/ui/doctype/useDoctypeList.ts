import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ActionContext,
  DialogRequest,
  Doctype,
  RowId,
  SortState,
} from './types'

const DEFAULT_LIMIT = 100

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

export function useDoctypeList<Row, Filters extends object, Detail>(
  doctype: Doctype<Row, Filters, Detail>,
  limit = DEFAULT_LIMIT,
) {
  const [rows, setRows] = useState<Row[]>([])
  const [filters, setFilters] = useState<Filters>(doctype.initialFilters)
  const [sort, setSortState] = useState<SortState | null>(null)
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [activeId, setActiveId] = useState<RowId | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<RowId>>(() => new Set())
  const [detail, setDetail] = useState<Detail | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [dialog, setDialog] = useState<DialogRequest | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const loadSequence = useRef(0)

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
    const controller = new AbortController()
    const sequence = ++loadSequence.current
    setLoading(true)
    setError('')

    void doctype.dataSource
      .list(
        {
          limit,
          offset,
          filters,
          sortBy: sort?.sortBy,
          sortDir: sort?.sortDir,
        },
        controller.signal,
      )
      .then((result) => {
        if (sequence !== loadSequence.current) return
        setRows(result.rows ?? [])
        setTotal(result.total ?? 0)
        setSelectedIds((current) => {
          const visible = new Set((result.rows ?? []).map(rowId))
          return new Set([...current].filter((id) => visible.has(id)))
        })
        setActiveId((current) => {
          if (current != null && result.rows?.some((row) => rowId(row) === current)) return current
          return result.rows?.[0] ? rowId(result.rows[0]) : null
        })
      })
      .catch((loadError: unknown) => {
        if (!controller.signal.aborted && sequence === loadSequence.current) {
          setError(errorMessage(loadError))
        }
      })
      .finally(() => {
        if (sequence === loadSequence.current) setLoading(false)
      })

    return () => controller.abort()
  }, [doctype.dataSource, filters, limit, offset, reloadKey, rowId, sort])

  useEffect(() => {
    if (!activeRow || !doctype.dataSource.detail) {
      setDetail(null)
      return
    }

    const controller = new AbortController()
    setDetailLoading(true)
    setError('')
    void doctype.dataSource
      .detail(rowId(activeRow), controller.signal)
      .then(setDetail)
      .catch((detailError: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(detailError))
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false)
      })
    return () => controller.abort()
  }, [activeRow, doctype.dataSource, rowId])

  const setFilter = useCallback(<Key extends keyof Filters>(key: Key, value: Filters[Key]) => {
    setFilters((current) => ({ ...current, [key]: value }))
    setOffset(0)
  }, [])

  const setSort = useCallback((sortBy: string) => {
    setSortState((current) => ({
      sortBy,
      sortDir: current?.sortBy === sortBy && current.sortDir === 'asc' ? 'desc' : 'asc',
    }))
    setOffset(0)
  }, [])

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
    if (!action || action.enabled?.(actionContext) === false) return
    if (action.confirm && !window.confirm(action.confirm)) return

    setLoading(true)
    setError('')
    setMessage('')
    try {
      const result = await action.run(actionContext)
      setMessage(result.message ?? '')
      setError(result.error ?? '')
      setDialog(result.open ?? null)
      if (result.reload) reload()
    } catch (actionError) {
      setError(errorMessage(actionError))
    } finally {
      setLoading(false)
    }
  }, [actionContext, doctype.actions, reload])

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return {
    rows,
    activeRow,
    activeId,
    setActiveId,
    detail,
    filters,
    setFilter,
    sort,
    setSort,
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
    loading,
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

