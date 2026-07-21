import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  DbrProgram,
  DbrPurchaseLaunchResult,
  DbrPurchasePlanPreview,
} from '../../domain/dbr'
import { dateRu, qty } from '../../lib/format'
import {
  listDbrPrograms,
  materializeDbrPurchasePlan,
  previewDbrPurchasePlan,
} from '../../services/dbr'
import { DbrConfirmDialog } from '../dbr/DbrConfirmDialog'
import { DbrNav } from '../dbr/DbrNav'
import { DbrPurchaseResultBody } from '../dbr/DbrPurchaseResultBody'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import {
  countPurchaseRowsWithinHorizon,
  type DbrPurchaseSortKey,
  purchaseRowClass,
  purchaseSortableClass,
  purchaseSourceFromKey,
  purchaseSourceParams,
  selectPurchaseRows,
} from './dbr-purchase/model'

export function DbrPurchasePage() {
  const [programs, setPrograms] = useState<DbrProgram[]>([])
  const [sourceKey, setSourceKey] = useState<string>('active')
  const [thresholdDays, setThresholdDays] = useState(60)
  const [preview, setPreview] = useState<DbrPurchasePlanPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [sort, setSort] = useState<DbrPurchaseSortKey>('order_before')
  const [onlyToOrder, setOnlyToOrder] = useState(true)

  // Two-step supplier-order materialization: dry-run preview → confirmed write.
  const [flow, setFlow] = useState<{
    preview?: DbrPurchaseLaunchResult
    result?: DbrPurchaseLaunchResult
  } | null>(null)
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
    } catch (e) {
      if (request === previewRequestRef.current) {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (request === previewRequestRef.current) setLoading(false)
    }
  }, [source, thresholdDays])

  const rows = useMemo(
    () => selectPurchaseRows(preview?.rows ?? [], onlyToOrder, sort),
    [preview, sort, onlyToOrder],
  )

  async function startMaterialize() {
    setFlow({})
    setFlowBusy(true)
    setFlowError('')
    try {
      const res = await materializeDbrPurchasePlan({ ...purchaseSourceParams(source, thresholdDays), dryRun: true })
      setFlow({ preview: res })
    } catch (e) {
      setFlow(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setFlowBusy(false)
    }
  }

  async function confirmMaterialize() {
    if (confirmBusyRef.current) return
    confirmBusyRef.current = true
    setFlowBusy(true)
    setFlowError('')
    try {
      const res = await materializeDbrPurchasePlan({ ...purchaseSourceParams(source, thresholdDays), dryRun: false })
      setFlow((prev) => (prev ? { ...prev, result: res } : prev))
      await loadPreview()
    } catch (e) {
      setFlowError(e instanceof Error ? e.message : String(e))
    } finally {
      confirmBusyRef.current = false
      setFlowBusy(false)
    }
  }

  const toOrderCount = preview?.rows_to_order ?? 0
  const withinHorizon = useMemo(() => countPurchaseRowsWithinHorizon(preview?.rows ?? []), [preview])

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование DBR / Закупка</div>
        <div className="runBadge">Чистая потребность → заказы поставщику</div>
      </div>

      <DocumentWindow
        title="Закупка под план"
        subtitle="Нетто-потребность по программе или активному графику: что и когда заказать поставщику"
        hotkeys="F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={rows.length ? 1 : 0}
            visibleTo={rows.length}
            total={preview?.items_total ?? 0}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <DbrNav />

        <div className="commandBar dbrFeederBar">
          <label className="inlineControl">
            <span>Источник</span>
            <select value={sourceKey} onChange={(e) => setSourceKey(e.target.value)}>
              <option value="active">Активный график барабана</option>
              {programs.map((p) => (
                <option key={p.id} value={p.id}>
                  Программа №{p.id} · {p.title || 'без названия'} · {dateRu(p.from_date)}—{dateRu(p.to_date)}
                </option>
              ))}
            </select>
          </label>
          <label className="inlineControl">
            <span>Горизонт заказа, дней</span>
            <input
              type="number"
              min={0}
              value={thresholdDays}
              onChange={(e) => setThresholdDays(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 80 }}
            />
          </label>
          <button className="primary" onClick={() => void loadPreview()} disabled={loading}>Рассчитать</button>
          <div className="commandBarSpacer" />
          <label className="dialogCheckRow">
            <input type="checkbox" checked={onlyToOrder} onChange={(e) => setOnlyToOrder(e.target.checked)} />
            Только к заказу
          </label>
          <button
            className="dbrDanger"
            onClick={() => void startMaterialize()}
            disabled={loading || flowBusy || !toOrderCount}
            title="Сформировать заказы поставщику по строкам «к заказу»"
          >
            Сформировать заказы…
          </button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        <div className="dbrFeederNotice">Расчёт потребности — только чтение. Формирование заказов создаёт документы «Заказ поставщику» в живой 1С и требует подтверждения.</div>

        {preview && (
          <div className="dbrKpis dbrFeederKpis">
            <div className="dbrKpi"><div className="dbrKpiLabel">Позиций в плане</div><div className="dbrKpiValue">{preview.items_total}</div><div className="dbrKpiSub">{preview.source.kind === 'active' ? 'активный график' : `программа №${preview.source.program_id}`}</div></div>
            <div className="dbrKpi"><div className="dbrKpiLabel">К заказу</div><div className="dbrKpiValue">{toOrderCount}</div><div className="dbrKpiSub">дефицит после запасов и открытых заказов</div></div>
            <div className={`dbrKpi ${withinHorizon ? 'alert' : ''}`}><div className="dbrKpiLabel">В горизонте {preview.lead_time_threshold_days} дн.</div><div className="dbrKpiValue">{withinHorizon}</div><div className="dbrKpiSub">срок заказа уже наступает</div></div>
          </div>
        )}

        {preview && !!preview.warnings.length && (
          <details className="dbrPayloadDetails">
            <summary>Предупреждения качества: {preview.warnings.length}</summary>
            <ul>{preview.warnings.slice(0, 100).map((w) => <li key={w}>{w}</li>)}</ul>
          </details>
        )}

        <div className="dbrFeederTableWrap">
          <table className="journalTable dbrTable dbrPurchaseTable">
            <thead>
              <tr>
                <th className={purchaseSortableClass(sort, 'item_code')} onClick={() => setSort('item_code')}>Позиция</th>
                <th className="numCell">Потребность</th>
                <th className="numCell">Запас</th>
                <th className="numCell">Открытый заказ</th>
                <th className="numCell">Доступно</th>
                <th className={`numCell ${purchaseSortableClass(sort, 'to_order_qty')}`} onClick={() => setSort('to_order_qty')}>Заказать</th>
                <th className={purchaseSortableClass(sort, 'order_before')} onClick={() => setSort('order_before')}>Заказать до</th>
                <th>Дата потребности</th>
                <th>Поставщик</th>
                <th>В горизонте</th>
              </tr>
            </thead>
            <tbody>
              {!loading && !preview && <tr><td colSpan={10} className="emptyCell">Выберите источник и нажмите «Рассчитать».</td></tr>}
              {!loading && preview && !rows.length && <tr><td colSpan={10} className="emptyCell">Нет позиций{onlyToOrder ? ' к заказу' : ''}.</td></tr>}
              {rows.map((row) => (
                <tr key={row.item_id} className={purchaseRowClass(row)}>
                  <td><strong>{row.item_code}</strong><span className="dbrFeederItemName">{row.item_name}</span></td>
                  <td className="numCell">{qty(row.demand_qty)}</td>
                  <td className="numCell">{qty(row.stock_qty)}</td>
                  <td className="numCell">{qty(row.open_order_qty)}</td>
                  <td className="numCell">{qty(row.available_qty)}</td>
                  <td className="numCell"><strong>{qty(row.to_order_qty)}</strong></td>
                  <td>{dateRu(row.order_before) || '—'}</td>
                  <td>{dateRu(row.need_date) || '—'}</td>
                  <td>{row.supplier_ref1c ? row.supplier_ref1c : <span className="dbrQualityWarning" title="Поставщик не назначен — строка не попадёт в заказ">⚠ не назначен</span>}</td>
                  <td>{row.within_lead_time_threshold ? <span className="dbrZoneBadge red"><span className="dbrDot r" />да</span> : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>

      {/* ── Materialize supplier orders: preview → confirm ─────────────── */}
      {flow && (
        <DbrConfirmDialog
          title="Формирование заказов поставщику"
          phase={flow.result ? 'done' : 'preview'}
          busy={flowBusy}
          confirmLabel="Провести в 1С"
          error={flowError}
          onClose={() => setFlow(null)}
          onConfirm={() => void confirmMaterialize()}
        >
          {(() => {
            const data = flow.result ?? flow.preview
            if (!data) return <div className="fieldHint">Загрузка предпросмотра…</div>
            return <DbrPurchaseResultBody data={data} />
          })()}
        </DbrConfirmDialog>
      )}
    </main>
  )
}
