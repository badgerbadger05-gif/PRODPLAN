import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  purchaseLineStatusLabel,
  type PurchaseFilters,
  type PurchaseJournalSummary,
  type PurchaseRow,
  type PurchaseSupplierOption,
} from '../../domain/purchaseControl'
import {
  exportPurchasesTo1C,
  getPurchaseFilters,
  listPurchaseJournal,
  syncSupplierOrdersFrom1C,
} from '../../services/purchaseControl'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { PurchaseCommandBar } from './purchase-control/PurchaseCommandBar'
import { PurchaseDetailPane } from './purchase-control/PurchaseDetailPane'
import { PurchaseFilterBar } from './purchase-control/PurchaseFilterBar'
import { PurchaseOrdersTable } from './purchase-control/PurchaseOrdersTable'
import type { PurchaseOrderSortKey } from './purchase-control/purchaseOrdersDoctype'

const limit = 100

const csvColumns: Array<[string, (row: PurchaseRow) => string | number]> = [
  ['Заказ', (row) => row.line_status === 'to_order' ? `MRP #${row.purchase_id}` : row.order_number],
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
  ['Статус', (row) => purchaseLineStatusLabel(row.line_status)],
  ['Сумма', (row) => row.amount || ''],
]

export function PurchaseControlPage() {
  const [searchParams] = useSearchParams()
  const focusOrderId = searchParams.get('order_id')
  const focusSearch = searchParams.get('search')
  const [rows, setRows] = useState<PurchaseRow[]>([])
  const [summary, setSummary] = useState<PurchaseJournalSummary | null>(null)
  const [selectedPurchaseIds, setSelectedPurchaseIds] = useState<Set<number>>(new Set())
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [total, setTotal] = useState(0)
  const [runId, setRunId] = useState<number | null>(null)
  const [offset, setOffset] = useState(0)
  const [suppliers, setSuppliers] = useState<PurchaseSupplierOption[]>([])
  const [states, setStates] = useState<string[]>([])
  const [filters, setFilters] = useState<PurchaseFilters>({
    search: focusSearch ?? '',
    supplier_id: '',
    line_status: '',
    state: '',
    active_only: true,
    sort_by: 'delivery_date',
    sort_dir: 'asc',
  })
  const filtersRef = useRef(filters)

  useEffect(() => {
    filtersRef.current = filters
  }, [filters])

  const activeRow = useMemo(() => rows.find((row) => row.row_key === activeKey) ?? rows[0] ?? null, [rows, activeKey])
  const toOrderRows = useMemo(() => rows.filter((row) => row.line_status === 'to_order' && row.purchase_id !== null), [rows])

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const current = filtersRef.current
      const params = new URLSearchParams()
      params.set('limit', String(limit))
      params.set('offset', String(nextOffset))
      if (focusOrderId) params.set('order_id', focusOrderId)
      if (current.search) params.set('search', current.search)
      if (current.supplier_id) params.set('supplier_id', current.supplier_id)
      if (current.line_status) params.set('line_status', current.line_status)
      if (current.state) params.set('state', current.state)
      params.set('active_only', current.active_only ? 'true' : 'false')
      params.set('sort_by', current.sort_by)
      params.set('sort_dir', current.sort_dir)
      const data = await listPurchaseJournal(params)
      setRows(data.rows ?? [])
      setTotal(data.total ?? 0)
      setRunId(data.run_id ?? null)
      setSummary(data.summary ?? null)
      setOffset(data.offset ?? nextOffset)
      setActiveKey((currentKey) => {
        if (currentKey && data.rows?.some((row) => row.row_key === currentKey)) return currentKey
        return data.rows?.[0]?.row_key ?? null
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [focusOrderId])

  const loadFilters = useCallback(async () => {
    try {
      const data = await getPurchaseFilters()
      setSuppliers(data.suppliers ?? [])
      setStates(data.states ?? [])
    } catch {
      // справочники фильтров не критичны для отображения журнала
    }
  }, [])

  useEffect(() => {
    void load(0)
    void loadFilters()
  }, [load, loadFilters])

  function changeFilters(next: PurchaseFilters, submit = false) {
    filtersRef.current = next
    setFilters(next)
    if (submit) void load(0)
  }

  function toggleSort(key: PurchaseOrderSortKey) {
    const sortBy = key === 'order' ? 'order_date' : 'delivery_date'
    const current = filtersRef.current
    const next = {
      ...current,
      sort_by: sortBy,
      sort_dir: current.sort_by === sortBy && current.sort_dir === 'asc' ? 'desc' : 'asc',
    } satisfies PurchaseFilters
    filtersRef.current = next
    setFilters(next)
    void load(0)
  }

  function showStatus(status: string) {
    const current = filtersRef.current
    changeFilters({ ...current, line_status: current.line_status === status ? '' : status }, true)
  }

  async function orderTo1C() {
    if (!runId) {
      setError('Нет зафиксированного MRP-прогона: нечего заказывать')
      return
    }
    const ids = Array.from(selectedPurchaseIds)
    if (!ids.length) return
    setLoading(true)
    setError('')
    try {
      const result = await exportPurchasesTo1C(runId, ids)
      const created = Number(result.orders_created ?? 0)
      const existing = Number(result.orders_existing ?? 0)
      setMessage(`Заказы поставщику: создано ${created}, уже было ${existing}`)
      setSelectedPurchaseIds(new Set())
      await syncSupplierOrdersFrom1C().catch(() => undefined)
      await load(offset)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setLoading(false)
    }
  }

  async function syncFrom1C() {
    setLoading(true)
    setError('')
    try {
      const stats = await syncSupplierOrdersFrom1C()
      const updated = Number(stats.orders_updated ?? 0)
      const created = Number(stats.orders_created ?? 0)
      setMessage(`Синхронизация: новых заказов ${created}, обновлено ${updated}`)
      await load(offset)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setLoading(false)
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
            selectedCount={selectedPurchaseIds.size}
            canPrev={offset > 0}
            canNext={offset + rows.length < total}
            onPrev={() => void load(Math.max(0, offset - limit))}
            onNext={() => void load(offset + limit)}
          />
        )}
      >
        <PurchaseCommandBar
          selectedCount={selectedPurchaseIds.size}
          toOrderCount={toOrderRows.length}
          summary={summary}
          loading={loading}
          onOrderTo1C={() => void orderTo1C()}
          onSyncFrom1C={() => void syncFrom1C()}
          onDownloadCsv={downloadCsv}
          onRefresh={() => void load(offset)}
          onSelectAllToOrder={() => setSelectedPurchaseIds(new Set(toOrderRows.map((row) => row.purchase_id as number)))}
          onClearSelection={() => setSelectedPurchaseIds(new Set())}
          onShowStatus={showStatus}
        />

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split">
          <div className="tablePane">
            <PurchaseFilterBar
              filters={filters}
              suppliers={suppliers}
              states={states}
              onChange={changeFilters}
              onSubmit={() => void load(0)}
            />
            <PurchaseOrdersTable
              rows={rows}
              activeRow={activeRow}
              selectedPurchaseIds={selectedPurchaseIds}
              sort={{ sortBy: filters.sort_by === 'order_date' ? 'order' : 'delivery_date', sortDir: filters.sort_dir }}
              onSelectPurchaseIds={setSelectedPurchaseIds}
              onActivate={setActiveKey}
              onToggleSort={toggleSort}
            />
          </div>

          <PurchaseDetailPane activeRow={activeRow} />
        </div>
      </DocumentWindow>
    </main>
  )
}
