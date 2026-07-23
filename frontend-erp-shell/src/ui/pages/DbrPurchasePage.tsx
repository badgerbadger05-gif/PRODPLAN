import type { KeyboardEvent } from 'react'
import { dateRu, qty } from '../../lib/format'
import { DbrNav } from '../dbr/DbrNav'
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
    cockpit, loading, error, sort, setSort, onlyToOrder, setOnlyToOrder,
    rows, toOrderCount, loadCockpit,
  } = useDbrPurchaseController()
  const meta = cockpit?.meta
  const itemTotal = cockpit?.rows.length ?? 0

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
        subtitle="Сохранённые незакрытые обязательства Ledger: что и когда заказать поставщику"
        hotkeys="F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={rows.length ? 1 : 0}
            visibleTo={rows.length}
            total={itemTotal}
            selectedCount={0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <DbrNav />

        <div className="commandBar dbrFeederBar" role="group" aria-label="Сохранённый снимок закупки">
          <button className="secondary" onClick={() => void loadCockpit()} disabled={loading}>Обновить снимок</button>
          <div className="commandBarSpacer" />
          <label className="dialogCheckRow">
            <input type="checkbox" checked={onlyToOrder} onChange={(e) => setOnlyToOrder(e.target.checked)} />
            Только к заказу
          </label>
        </div>

        {loading && <div className="srOnly" role="status">Загрузка сохранённого снимка закупки…</div>}
        {error && <div className="errorLine" role="alert">{error}</div>}
        <div className="dbrFeederNotice">Только чтение: страница показывает сохранённый снимок незакрытых Ledger-обязательств. Расчёт и формирование заказов выполняются фоновым контуром, не при открытии страницы.</div>

        {cockpit && (
          <div className="dbrKpis dbrFeederKpis">
            <div className="dbrKpi"><div className="dbrKpiLabel">Позиций в снимке</div><div className="dbrKpiValue">{itemTotal}</div><div className="dbrKpiSub">незакрытые обязательства Ledger</div></div>
            <div className="dbrKpi"><div className="dbrKpiLabel">К заказу</div><div className="dbrKpiValue">{toOrderCount}</div><div className="dbrKpiSub">непокрытые Ledger-обязательства</div></div>
          </div>
        )}

        {cockpit && (
          <div className="dbrFeederNotice" data-testid="purchase-snapshot-lineage">
            Снимок #{meta?.snapshot_id ?? '—'} · Ledger-поколение #{meta?.ledger_generation ?? '—'} · срез {dateRu(meta?.cutoff) || '—'} · MRP: {meta?.runs?.length ? meta.runs.map((run) => `run #${run.run_id} · freeze ${run.freeze_version}`).join('; ') : '—'}
          </div>
        )}

        <div className="dbrFeederTableWrap">
          <table className="journalTable dbrTable dbrPurchaseTable" aria-busy={loading}>
            <caption className="srOnly">Сохранённые Ledger-обязательства закупки</caption>
            <thead>
              <tr>
                <th scope="col" tabIndex={0} aria-sort={ariaSort('item_code')} className={purchaseSortableClass(sort, 'item_code')} onClick={() => setSort('item_code')} onKeyDown={(event) => handleSortKeyDown(event, 'item_code')}>Позиция</th>
                <th scope="col" className="numCell">Обязательство</th>
                <th scope="col" className="numCell">Ledger-запас</th>
                <th scope="col" className="numCell">Точное поступление</th>
                <th scope="col" tabIndex={0} aria-sort={ariaSort('to_order_qty')} className={`numCell ${purchaseSortableClass(sort, 'to_order_qty')}`} onClick={() => setSort('to_order_qty')} onKeyDown={(event) => handleSortKeyDown(event, 'to_order_qty')}>Непокрыто / заказать</th>
                <th scope="col" tabIndex={0} aria-sort={ariaSort('need_date')} className={purchaseSortableClass(sort, 'need_date')} onClick={() => setSort('need_date')} onKeyDown={(event) => handleSortKeyDown(event, 'need_date')}>Ранний период</th>
                <th scope="col">Пул / склад</th>
                <th scope="col">Поставщик</th>
              </tr>
            </thead>
            <tbody>
              {!loading && !cockpit && <tr><td colSpan={8} className="emptyCell">Сохранённый снимок закупки недоступен.</td></tr>}
              {!loading && cockpit && !rows.length && <tr><td colSpan={8} className="emptyCell">Нет позиций{onlyToOrder ? ' к заказу' : ''}.</td></tr>}
              {rows.map((row) => (
                <tr key={row.item_id} className={purchaseRowClass(row)}>
                  <td><strong>{row.item_code}</strong><span className="dbrFeederItemName">{row.item_name}</span></td>
                  <td className="numCell">{qty(row.outstanding_obligation_qty)}</td>
                  <td className="numCell">{qty(row.stock_qty)}</td>
                  <td className="numCell">{qty(row.exact_future_supply_qty)}</td>
                  <td className="numCell"><strong>{qty(row.to_order_qty)}</strong></td>
                  <td>{dateRu(row.need_date) || '—'}</td>
                  <td>{row.planning_stock_pool} / {row.warehouse_ref1c}</td>
                  <td>{row.supplier_ref1c ? row.supplier_ref1c : <span className="dbrQualityWarning" title="Поставщик не назначен — строка не попадёт в заказ">⚠ не назначен</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
