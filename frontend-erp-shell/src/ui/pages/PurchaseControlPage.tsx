import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  purchaseIdsForRow,
  purchaseLineStatusLabel,
  supplyPhaseLabel,
  type PurchaseFilters,
  type PurchaseJournalSummary,
  type PurchaseRow,
  type PurchaseSupplierOption,
} from '../../domain/purchaseControl'
import {
  exportPurchasesTo1C,
  getPurchaseFilters,
  syncSupplierOrdersFrom1C,
} from '../../services/purchaseControl'
import { useDoctypeList } from '../doctype'
import type { AccessSubject } from '../doctype/permissions'
import { useOptionalSession } from '../session'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { PurchaseCommandBar } from './purchase-control/PurchaseCommandBar'
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

const csvColumns: Array<[string, (row: PurchaseRow) => string | number]> = [
  ['Заказ', (row) => row.line_status === 'to_order'
    ? purchaseIdsForRow(row).map((id) => `MRP #${id}`).join(', ')
    : row.order_number],
  ['Дата заказа', (row) => row.order_date ?? ''],
  ['Поставщик', (row) => row.supplier_name],
  ['Артикул', (row) => row.item_article ?? row.item_code],
  ['Номенклатура', (row) => row.item_name],
  ['Заказано', (row) => row.quantity],
  ['Поступило', (row) => row.received_qty],
  ['Осталось', (row) => row.remaining_qty],
  ['Дата поставки', (row) => row.delivery_date ?? row.need_date ?? ''],
  ['Просрочка, дн', (row) => row.overdue_days || ''],
  ['Статус 1С', (row) => row.order_state_name ?? ''],
  ['Фаза', (row) => supplyPhaseLabel(row.supply_phase)],
  ['Статус', (row) => purchaseLineStatusLabel(row.line_status)],
  ['Сумма', (row) => row.amount || ''],
]

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
  const rows = journal.rows
  const summary = (journal.listMeta.summary as PurchaseJournalSummary | undefined) ?? null
  const [selectedPurchaseRowKeys, setSelectedPurchaseRowKeys] = useState<Set<string>>(new Set())
  const [commandLoading, setCommandLoading] = useState(false)
  const loading = journal.loading || commandLoading
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const total = journal.paging.total
  const runId = (journal.listMeta.run_id as number | null | undefined) ?? null
  const offset = journal.paging.offset
  const [suppliers, setSuppliers] = useState<PurchaseSupplierOption[]>([])
  const [states, setStates] = useState<string[]>([])
  const filters = journal.filters

  const activeRow = journal.activeRow
  const toOrderRows = useMemo(
    () => rows.filter((row) => row.line_status === 'to_order' && purchaseIdsForRow(row).length > 0),
    [rows],
  )

  useEffect(() => {
    const visibleRowKeys = new Set(
      rows
        .filter((row) => row.line_status === 'to_order' && purchaseIdsForRow(row).length > 0)
        .map((row) => row.row_key),
    )
    setSelectedPurchaseRowKeys((current) => {
      const pruned = new Set([...current].filter((rowKey) => visibleRowKeys.has(rowKey)))
      return pruned.size === current.size ? current : pruned
    })
  }, [rows])

  const loadFilters = useCallback(async () => {
    try {
      const data = await getPurchaseFilters()
      setSuppliers(data.suppliers ?? [])
      setStates(data.states ?? [])
    } catch {
      // справочники фильтров не критичны для отображения журнала
    }
  }, [])

  useEffect(() => { void loadFilters() }, [loadFilters])

  function changeFilters(next: PurchaseFilters, submit = false) {
    for (const key of Object.keys(next) as Array<keyof PurchaseFilters>) {
      if (next[key] !== filters[key]) journal.setFilter(key, next[key])
    }
    // Select/toggle definitions apply immediately in the runtime. Search remains
    // explicit and is submitted by PurchaseFilterBar via applyFilters().
    void submit
  }

  function toggleSort(key: PurchaseOrderSortKey) {
    journal.setSort(key)
  }

  function showStatus(status: string) {
    changeFilters({ ...filters, line_status: filters.line_status === status ? '' : status }, true)
  }

  function showPhase(phase: string) {
    changeFilters({ ...filters, phase: filters.phase === phase ? '' : phase }, true)
  }

  async function orderTo1C() {
    if (!runId) {
      setError('Нет зафиксированного MRP-прогона: нечего заказывать')
      return
    }
    const ids = [...new Set(
      toOrderRows
        .filter((row) => selectedPurchaseRowKeys.has(row.row_key))
        .flatMap(purchaseIdsForRow),
    )]
    if (!ids.length) return
    setCommandLoading(true)
    setError('')
    try {
      const result = await exportPurchasesTo1C(runId, ids)
      const created = Number(result.orders_created ?? 0)
      const existing = Number(result.orders_existing ?? 0)
      setMessage(`Заказы поставщику: создано ${created}, уже было ${existing}`)
      setSelectedPurchaseRowKeys(new Set())
      await syncSupplierOrdersFrom1C().catch(() => undefined)
      journal.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCommandLoading(false)
    }
  }

  async function syncFrom1C() {
    setCommandLoading(true)
    setError('')
    try {
      const stats = await syncSupplierOrdersFrom1C()
      const updated = Number(stats.orders_updated ?? 0)
      const created = Number(stats.orders_created ?? 0)
      setMessage(`Синхронизация: новых заказов ${created}, обновлено ${updated}`)
      journal.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCommandLoading(false)
    }
  }

  function downloadCsv() {
    const header = csvColumns.map(([title]) => title)
    const lines = [header, ...rows.map((row) => csvColumns.map(([, value]) => String(value(row))))]
    const csv = lines.map((cells) => cells.map((cell) => `"${cell.replaceAll('"', '""')}"`).join(';')).join('\n')
    const blob = new Blob(['\ufeff', csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'purchase-journal.csv'
    link.click()
    URL.revokeObjectURL(url)
  }

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Закупки / Журнал закупок</div>
        <div className="runBadge">MRP run: {runId ?? '—'}</div>
      </div>

      <DocumentWindow
        title="Журнал закупок"
        subtitle="Заказы поставщику из 1С и незаказанные MRP-потребности: сроки, поступления, просрочка"
        hotkeys="F5 Обновить · Enter Детали"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={selectedPurchaseRowKeys.size}
            canPrev={offset > 0}
            canNext={offset + rows.length < total}
            onPrev={journal.paging.prev}
            onNext={journal.paging.next}
          />
        )}
      >
        <PurchaseCommandBar
          selectedCount={selectedPurchaseRowKeys.size}
          toOrderCount={toOrderRows.length}
          summary={summary}
          activePhase={filters.phase}
          loading={loading}
          onOrderTo1C={() => void orderTo1C()}
          onSyncFrom1C={() => void syncFrom1C()}
          onDownloadCsv={downloadCsv}
          onRefresh={journal.reload}
          onSelectAllToOrder={() => setSelectedPurchaseRowKeys(new Set(toOrderRows.map((row) => row.row_key)))}
          onClearSelection={() => setSelectedPurchaseRowKeys(new Set())}
          onShowStatus={showStatus}
          onShowPhase={showPhase}
        />

        {(error || journal.error) && <div className="errorLine">{error || journal.error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split">
          <div className="tablePane">
            <PurchaseFilterBar
              filters={filters}
              suppliers={suppliers}
              states={states}
              onChange={changeFilters}
              onSubmit={journal.applyFilters}
            />
            <PurchaseOrdersTable
              rows={rows}
              activeRow={activeRow}
              selectedPurchaseRowKeys={selectedPurchaseRowKeys}
              sort={{
                sortBy: journal.sort?.sortBy === 'order' ? 'order' : 'delivery_date',
                sortDir: journal.sort?.sortDir ?? 'asc',
              }}
              onSelectPurchaseRowKeys={setSelectedPurchaseRowKeys}
              onActivate={journal.setActiveId}
              onToggleSort={toggleSort}
            />
          </div>

          <PurchaseDetailPane activeRow={activeRow} />
        </div>
      </DocumentWindow>
    </main>
  )
}
