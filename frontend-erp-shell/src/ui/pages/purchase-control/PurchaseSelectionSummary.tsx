import { useEffect, useState } from 'react'
import type { PurchaseSelectionSummary as SelectionSummary } from '../../../domain/purchaseControl'
import { qty } from '../../../lib/format'
import { getPurchaseSelectionSummary } from '../../../services/purchaseControl'

type Props = {
  currentScopeId: number | null
  sourceRevision: string
  selectionKey: string
  horizonPeriodTo: string
}

export function PurchaseSelectionSummary({ currentScopeId, sourceRevision, selectionKey, horizonPeriodTo }: Props) {
  const [summary, setSummary] = useState<SelectionSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!sourceRevision || !selectionKey || !currentScopeId) {
      setSummary(null)
      setLoading(false)
      setError('')
      return
    }

    const controller = new AbortController()
    setSummary(null)
    setLoading(true)
    setError('')
    getPurchaseSelectionSummary({
      current_scope_id: currentScopeId,
      current_identities: selectionKey.split('\u001f'),
      expected_source_revision: sourceRevision,
      horizon_period_to: horizonPeriodTo || null,
    }, controller.signal)
      .then((data) => setSummary(data))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setSummary(null)
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [currentScopeId, horizonPeriodTo, selectionKey, sourceRevision])

  if (!selectionKey) return null
  if (loading && !summary) return <span className="toolbarText">Итог выбранного: расчёт…</span>
  if (error) return <span className="toolbarText" title={error}>Итог выбранного: н/д</span>
  if (!summary) return null

  return (
    <>
      <div className="barSeparator" />
      <span className="toolbarText">Выбрано: {summary.selected_rows}</span>
      {summary.amount_status === 'complete' ? (
        <span className="toolbarText">Сумма: {qty(summary.total_amount ?? 0)}</span>
      ) : summary.amount_status === 'partial' ? (
        <span className="toolbarText">
          Известная сумма: {qty(summary.known_amount)} · без цены: {summary.unpriced_rows}
        </span>
      ) : (
        <span className="toolbarText">Сумма: н/д · без цены: {summary.unpriced_rows}</span>
      )}
    </>
  )
}
