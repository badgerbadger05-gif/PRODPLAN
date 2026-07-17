import type { DbrPurchaseLaunchResult } from '../../domain/dbr'
import { dateRu, qty } from '../../lib/format'

// Shared body for the supplier-order flows (/feeder/purchase/launch and
// /purchase-plan/materialize): supplier groups + unresolved + already-exported.
// Reused by the feeder queue and the purchase tab so both look identical.
export function DbrPurchaseResultBody({ data }: { data: DbrPurchaseLaunchResult }) {
  const done = !data.dry_run
  return (
    <>
      <div className="dbrDaySummaryLine">
        Заказов: {data.orders_planned}
        {done ? ` · создано: ${data.orders_created}${data.errors ? ` · ошибок: ${data.errors}` : ''}` : ''}
        {data.unresolved.length ? ` · без поставщика: ${data.unresolved.length}` : ''}
        {data.already_exported.length ? ` · уже заказано: ${data.already_exported.length}` : ''}
      </div>

      {data.orders.map((group) => (
        <div
          key={group.number}
          className={`dbrPoGroup${group.status === 'error' ? ' error' : group.status === 'created' ? ' created' : ''}`}
        >
          <div className="dbrPoGroupHead">
            <strong>Поставщик {group.supplier_ref1c}</strong>
            <span>
              Документ № {group.number}
              {group.status ? ` · ${group.status}` : ''}
              {group.error ? ` · ${group.error}` : ''}
            </span>
          </div>
          <table className="dbrDeficitLinesTable">
            <thead>
              <tr><th>Номенклатура</th><th className="numCell">Кол-во</th><th>Заказать до</th><th>Приход к</th></tr>
            </thead>
            <tbody>
              {group.lines.map((line) => (
                <tr key={line.item_id}>
                  <td>{line.item_name || `#${line.item_id}`}</td>
                  <td className="numCell">{qty(line.qty)}</td>
                  <td>{dateRu(line.order_date) || '—'}</td>
                  <td>{dateRu(line.need_date) || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {!data.orders.length && <div className="fieldHint">Нет строк для заказа поставщику.</div>}

      {data.unresolved.length > 0 && (
        <details className="dbrPayloadDetails" open>
          <summary>Без поставщика / без кода 1С: {data.unresolved.length}</summary>
          <table className="dbrDeficitLinesTable">
            <thead><tr><th>Позиция</th><th>Причина</th></tr></thead>
            <tbody>
              {data.unresolved.map((u, i) => (
                <tr key={`${u.item_id}-${u.signal_id ?? i}`}>
                  <td>{u.item_name || `#${u.item_id}`}</td>
                  <td>
                    {[u.missing_supplier ? 'нет поставщика' : null, u.missing_item_ref1c ? 'нет кода 1С' : null]
                      .filter(Boolean)
                      .join(', ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {data.already_exported.length > 0 && (
        <details className="dbrPayloadDetails">
          <summary>Уже заказано ранее: {data.already_exported.length}</summary>
          <ul className="dbrPlainList">
            {data.already_exported.map((a, i) => (
              <li key={i}>
                {a.item_id ? `Поз. #${a.item_id}` : a.signal_id ? `Сигнал #${a.signal_id}` : '—'} → №{' '}
                {a.one_c_order_number ?? a.one_c_order_ref ?? '—'}
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}
