import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DbrPurchaseCockpit, DbrPurchaseCockpitRow } from '../../../domain/dbr'
import { dbrSnapshotUnavailableMessage, getDbrPurchaseCockpit } from '../../../services/dbr'
import {
  type DbrPurchaseSortKey,
  selectPurchaseRows,
} from './model'

export function useDbrPurchaseController() {
  const [cockpit, setCockpit] = useState<DbrPurchaseCockpit | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [sort, setSort] = useState<DbrPurchaseSortKey>('need_date')
  const [onlyToOrder, setOnlyToOrder] = useState(true)
  const requestRef = useRef(0)

  // Mount does exactly one GET of the saved envelope.  Filters below work on
  // its rows only and must never invoke a planning calculation.
  const loadCockpit = useCallback(async () => {
    const request = ++requestRef.current
    setLoading(true)
    setError('')
    try {
      const next = await getDbrPurchaseCockpit()
      if (request === requestRef.current) setCockpit(next)
    } catch (cause) {
      if (request === requestRef.current) {
        setCockpit(null)
        setError(dbrSnapshotUnavailableMessage(cause) ?? (cause instanceof Error ? cause.message : String(cause)))
      }
    } finally {
      if (request === requestRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => { void loadCockpit() }, [loadCockpit])

  const rows = useMemo(
    () => selectPurchaseRows(cockpit?.rows ?? [], onlyToOrder, sort),
    [cockpit, sort, onlyToOrder],
  )
  const sourceRows = useMemo<readonly DbrPurchaseCockpitRow[]>(() => cockpit?.rows ?? [], [cockpit])
  const toOrderCount = sourceRows.filter((row) => row.to_order_qty > 0).length

  return {
    cockpit,
    loading,
    error,
    sort,
    setSort,
    onlyToOrder,
    setOnlyToOrder,
    rows,
    toOrderCount,
    loadCockpit,
  }
}
