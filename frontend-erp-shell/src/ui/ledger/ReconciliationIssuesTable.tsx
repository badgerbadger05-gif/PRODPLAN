import { dateTimeRu, qty } from '../../lib/format'
import type { ReconciliationIssueView } from './types'
import { StatusBadge } from '../kit'

const severityLabels = {
  info: 'Информация',
  warning: 'Предупреждение',
  error: 'Ошибка',
}

const statusLabels = {
  open: 'Открыто',
  acknowledged: 'Принято',
  resolved: 'Исправлено',
}

export function ReconciliationIssuesTable({ rows }: { rows: ReconciliationIssueView[] }) {
  return (
    <table className="journalTable reconciliationTable">
      <thead>
        <tr>
          <th>Уровень</th>
          <th>Номенклатура</th>
          <th>Пул</th>
          <th className="numCell">Ledger</th>
          <th className="numCell">Проекция</th>
          <th className="numCell">Расхождение</th>
          <th>Обнаружено</th>
          <th>Статус</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <td><StatusBadge tone={row.severity === 'error' ? 'shortage' : row.severity === 'warning' ? 'to_move' : ''}>{severityLabels[row.severity]}</StatusBadge></td>
            <td>{row.itemLabel}</td>
            <td><code>{row.poolKey}</code></td>
            <td className="numCell">{qty(row.ledgerQuantity)}</td>
            <td className="numCell">{qty(row.projectionQuantity)}</td>
            <td className="numCell"><strong>{row.difference > 0 ? '+' : ''}{qty(row.difference)}</strong></td>
            <td>{dateTimeRu(row.detectedAt) || '—'}</td>
            <td>{statusLabels[row.status]}</td>
          </tr>
        ))}
        {!rows.length && (
          <tr><td colSpan={8}><div className="emptyDetail">Расхождений нет</div></td></tr>
        )}
      </tbody>
    </table>
  )
}
