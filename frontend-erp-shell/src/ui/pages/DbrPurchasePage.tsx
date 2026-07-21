import type { KeyboardEvent } from 'react'
import { dateRu, qty } from '../../lib/format'
import { DbrConfirmDialog } from '../dbr/DbrConfirmDialog'
import { DbrNav } from '../dbr/DbrNav'
import { DbrPurchaseResultBody } from '../dbr/DbrPurchaseResultBody'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import {
  type DbrPurchaseSortKey,
  purchaseRowClass,
  purchaseSortableClass,
} from './dbr-purchase/model'
import { useDbrPurchaseController } from './dbr-purchase/useDbrPurchaseController'

export function DbrPurchasePage() {
  const {
    programs, sourceKey, setSourceKey, thresholdDays, setThresholdDays,
    preview, loading, error, sort, setSort, onlyToOrder, setOnlyToOrder,
    flow, flowBusy, flowError, rows, toOrderCount, withinHorizon,
    loadPreview, startMaterialize, confirmMaterialize, closeFlow,
  } = useDbrPurchaseController()

  function handleSortKeyDown(
    event: KeyboardEvent<HTMLTableCellElement>,
    key: DbrPurchaseSortKey,
  ) {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    setSort(key)
  }

  function ariaSort(key: DbrPurchaseSortKey): 'ascending' | 'descending' | 'none' {
    if (sort !== key) return 'none'
    return key === 'to_order_qty' ? 'descending' : 'ascending'
  }

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

        <div className="commandBar dbrFeederBar" role="group" aria-label="Параметры расчёта закупки">
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

        {loading && <div className="srOnly" role="status">Загрузка плана закупки…</div>}
        {error && <div className="errorLine" role="alert">{error}</div>}
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
          <table className="journalTable dbrTable dbrPurchaseTable" aria-busy={loading}>
            <caption className="srOnly">План закупки по чистой потребности</caption>
            <thead>
              <tr>
                <th scope="col" tabIndex={0} aria-sort={ariaSort('item_code')} className={purchaseSortableClass(sort, 'item_code')} onClick={() => setSort('item_code')} onKeyDown={(event) => handleSortKeyDown(event, 'item_code')}>Позиция</th>
                <th scope="col" className="numCell">Потребность</th>
                <th scope="col" className="numCell">Запас</th>
                <th scope="col" className="numCell">Открытый заказ</th>
                <th scope="col" className="numCell">Доступно</th>
                <th scope="col" tabIndex={0} aria-sort={ariaSort('to_order_qty')} className={`numCell ${purchaseSortableClass(sort, 'to_order_qty')}`} onClick={() => setSort('to_order_qty')} onKeyDown={(event) => handleSortKeyDown(event, 'to_order_qty')}>Заказать</th>
                <th scope="col" tabIndex={0} aria-sort={ariaSort('order_before')} className={purchaseSortableClass(sort, 'order_before')} onClick={() => setSort('order_before')} onKeyDown={(event) => handleSortKeyDown(event, 'order_before')}>Заказать до</th>
                <th scope="col">Дата потребности</th>
                <th scope="col">Поставщик</th>
                <th scope="col">В горизонте</th>
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
          onClose={closeFlow}
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
