import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  supplyPhaseOptions,
  type PurchaseFilters,
  type PurchaseJournalSummary,
  type PurchaseSupplierOption,
} from '../../domain/purchaseControl'
import { getPurchaseFilters } from '../../services/purchaseControl'
import { DoctypePage, useDoctypeList } from '../doctype'
import type { AccessSubject } from '../doctype/permissions'
import { useOptionalSession } from '../session'
import { PurchaseDetailPane } from './purchase-control/PurchaseDetailPane'
import { PurchaseFilterBar } from './purchase-control/PurchaseFilterBar'
import { PurchaseOrdersTable } from './purchase-control/PurchaseOrdersTable'
import {
  createPurchaseOrdersDoctype,
  type PurchaseOrderSortKey,
} from './purchase-control/purchaseOrdersDoctype'

const limit = 100
const transitionalAccess: AccessSubject = {
  roles: ['buyer'],
  permissions: ['purchase.export_1c', 'purchase.sync_1c'],
}

export function PurchaseControlPage() {
  const session = useOptionalSession()
  const access = session?.user ?? transitionalAccess
  const [searchParams] = useSearchParams()
  const focusOrderId = searchParams.get('order_id')
  const focusSearch = searchParams.get('search')
  const doctype = useMemo(
    () => createPurchaseOrdersDoctype({ orderId: focusOrderId, search: focusSearch }),
    [focusOrderId, focusSearch],
  )
  const journal = useDoctypeList(doctype, { limit, access })
  const [suppliers, setSuppliers] = useState<PurchaseSupplierOption[]>([])
  const [states, setStates] = useState<string[]>([])

  const loadFilters = useCallback(async () => {
    try {
      const data = await getPurchaseFilters()
      setSuppliers(data.suppliers ?? [])
      setStates(data.states ?? [])
    } catch {
      // Справочники фильтров не блокируют чтение журнала.
    }
  }, [])

  useEffect(() => { void loadFilters() }, [loadFilters])

  const changeFilters = useCallback((next: PurchaseFilters) => {
    for (const key of Object.keys(next) as Array<keyof PurchaseFilters>) {
      if (next[key] !== journal.filters[key]) journal.setFilter(key, next[key])
    }
  }, [journal])

  const toggleFilter = useCallback((key: 'phase' | 'line_status', value: string) => {
    journal.setFilter(key, journal.filters[key] === value ? '' : value)
  }, [journal])

  return (
    <DoctypePage
      doctype={doctype}
      state={journal}
      access={access}
      breadcrumbs="Закупки / Журнал закупок"
      renderTopBadge={(state) => {
        const meta = state.listMeta.meta as { ledger_generation?: number; snapshot_id?: number } | undefined
        const snapshot = Number(meta?.snapshot_id ?? 0)
        const ledger = Number(meta?.ledger_generation ?? state.listMeta.ledger_generation_id)
        return (
          <>
            Снимок: {snapshot || '—'} · Ledger: {ledger || '—'}
          </>
        )
      }}
      renderFilters={() => null}
      renderTable={(state) => (
        <div className="tablePane">
          <PurchaseFilterBar
            filters={state.filters}
            suppliers={suppliers}
            states={states}
            onChange={changeFilters}
            onSubmit={state.applyFilters}
          />
          <PurchaseOrdersTable
            rows={state.rows}
            activeRow={state.activeRow}
            selectedPurchaseRowKeys={new Set([...state.selectedIds].map(String))}
            sort={{
              sortBy: state.sort?.sortBy === 'order' ? 'order' : 'delivery_date',
              sortDir: state.sort?.sortDir ?? 'asc',
            }}
            onSelectPurchaseRowKeys={(ids) => state.setSelection(ids)}
            onActivate={state.setActiveId}
            onToggleSort={(key: PurchaseOrderSortKey) => state.setSort(key)}
          />
        </div>
      )}
      renderDetail={(_, state) => <PurchaseDetailPane activeRow={state.activeRow} embedded />}
      renderToolbarAfter={(state) => {
        const summary = state.listMeta.summary as PurchaseJournalSummary | undefined
        const eligibleCount = state.rows.filter((row) => doctype.selectable?.(row) !== false).length
        return (
          <div className="commandBar purchaseSummaryBar">
            <button onClick={() => state.setVisibleSelection(true)} disabled={!eligibleCount}>
              Выбрать все «К заказу»
            </button>
            <button onClick={() => state.setVisibleSelection(false)} disabled={!state.selection.length}>
              Снять выбор
            </button>
            {summary && (
              <>
                <div className="barSeparator" />
                {supplyPhaseOptions.map(([value, label]) => (
                  <button
                    key={value}
                    className="filterBtn"
                    onClick={() => toggleFilter('phase', value)}
                    style={state.filters.phase === value ? { fontWeight: 700, textDecoration: 'underline' } : undefined}
                    title={`Показать только фазу «${label}»`}
                  >
                    {label}: {summary.by_phase?.[value] ?? 0}
                  </button>
                ))}
                <div className="barSeparator" />
                <button className="filterBtn" onClick={() => toggleFilter('line_status', 'to_order')}>
                  К заказу: {summary.to_order}
                </button>
                <button
                  className="filterBtn"
                  onClick={() => toggleFilter('line_status', 'overdue')}
                  style={summary.overdue > 0 ? { color: 'var(--red)' } : undefined}
                >
                  Просрочено: {summary.overdue}
                </button>
                <span className="toolbarText">Ожидается за 7 дн: {summary.expected_7d}</span>
                {summary.fact_status === 'unavailable' && (
                  <span className="toolbarText" title="Снимок не содержит подтверждённый факт поступления">
                    Факт поступления: н/д
                  </span>
                )}
              </>
            )}
          </div>
        )
      }}
    />
  )
}
