import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DbrProgram, DbrPurchaseLaunchResult, DbrPurchasePlanPreview } from '../../../domain/dbr'
import {
  listDbrPrograms,
  materializeDbrPurchasePlan,
  previewDbrPurchasePlan,
} from '../../../services/dbr'
import {
  countPurchaseRowsWithinHorizon,
  type DbrPurchaseSortKey,
  purchaseSourceFromKey,
  purchaseSourceParams,
  selectPurchaseRows,
} from './model'

type PurchaseFlow = {
  preview?: DbrPurchaseLaunchResult
  result?: DbrPurchaseLaunchResult
}

export function useDbrPurchaseController() {
  const [programs, setPrograms] = useState<DbrProgram[]>([])
  const [sourceKey, setSourceKey] = useState('active')
  const [thresholdDays, setThresholdDays] = useState(60)
  const [preview, setPreview] = useState<DbrPurchasePlanPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [sort, setSort] = useState<DbrPurchaseSortKey>('order_before')
  const [onlyToOrder, setOnlyToOrder] = useState(true)
  const [flow, setFlow] = useState<PurchaseFlow | null>(null)
  const [flowBusy, setFlowBusy] = useState(false)
  const [flowError, setFlowError] = useState('')
  const previewRequestRef = useRef(0)
  const confirmBusyRef = useRef(false)

  const source = useMemo(() => purchaseSourceFromKey(sourceKey), [sourceKey])

  useEffect(() => {
    let cancelled = false
    void listDbrPrograms()
      .then((list) => { if (!cancelled) setPrograms(list) })
      .catch(() => { if (!cancelled) setPrograms([]) })
    return () => { cancelled = true }
  }, [])

  const loadPreview = useCallback(async () => {
    const request = ++previewRequestRef.current
    setLoading(true)
    setError('')
    setPreview(null)
    try {
      const nextPreview = await previewDbrPurchasePlan(purchaseSourceParams(source, thresholdDays))
      if (request === previewRequestRef.current) setPreview(nextPreview)
    } catch (cause) {
      if (request === previewRequestRef.current) {
        setError(cause instanceof Error ? cause.message : String(cause))
      }
    } finally {
      if (request === previewRequestRef.current) setLoading(false)
    }
  }, [source, thresholdDays])

  const rows = useMemo(
    () => selectPurchaseRows(preview?.rows ?? [], onlyToOrder, sort),
    [preview, sort, onlyToOrder],
  )

  const startMaterialize = useCallback(async () => {
    setFlow({})
    setFlowBusy(true)
    setFlowError('')
    try {
      const result = await materializeDbrPurchasePlan({
        ...purchaseSourceParams(source, thresholdDays),
        dryRun: true,
      })
      setFlow({ preview: result })
    } catch (cause) {
      setFlow(null)
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setFlowBusy(false)
    }
  }, [source, thresholdDays])

  const confirmMaterialize = useCallback(async () => {
    if (confirmBusyRef.current) return
    confirmBusyRef.current = true
    setFlowBusy(true)
    setFlowError('')
    try {
      const result = await materializeDbrPurchasePlan({
        ...purchaseSourceParams(source, thresholdDays),
        dryRun: false,
      })
      setFlow((current) => (current ? { ...current, result } : current))
      await loadPreview()
    } catch (cause) {
      setFlowError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      confirmBusyRef.current = false
      setFlowBusy(false)
    }
  }, [loadPreview, source, thresholdDays])

  const closeFlow = useCallback(() => setFlow(null), [])
  const toOrderCount = preview?.rows_to_order ?? 0
  const withinHorizon = useMemo(
    () => countPurchaseRowsWithinHorizon(preview?.rows ?? []),
    [preview],
  )

  return {
    programs,
    sourceKey,
    setSourceKey,
    thresholdDays,
    setThresholdDays,
    preview,
    loading,
    error,
    sort,
    setSort,
    onlyToOrder,
    setOnlyToOrder,
    flow,
    flowBusy,
    flowError,
    rows,
    toOrderCount,
    withinHorizon,
    loadPreview,
    startMaterialize,
    confirmMaterialize,
    closeFlow,
  }
}
