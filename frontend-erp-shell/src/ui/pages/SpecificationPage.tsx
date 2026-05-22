import { useMemo, useState } from 'react'
import type { SpecFlatRow, SpecNode } from '../../domain/specification'
import { qty } from '../../lib/format'
import { getSpecificationFull } from '../../services/specification'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

function flatten(nodes: SpecNode[], level = 0): SpecFlatRow[] {
  return nodes.flatMap((node) => [
    { ...node, level },
    ...flatten(node.children ?? [], level + 1),
  ])
}

function nodeTitle(node: SpecNode) {
  return node.type === 'operation'
    ? node.operation?.name || 'Операция'
    : node.name || 'Номенклатура'
}

export function SpecificationPage() {
  const [itemCode, setItemCode] = useState('')
  const [rootQty, setRootQty] = useState(1)
  const [nodes, setNodes] = useState<SpecNode[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const rows = useMemo(() => flatten(nodes), [nodes])
  const warningsCount = rows.reduce((sum, row) => sum + (row.warnings?.length ?? 0), 0)

  async function load() {
    if (!itemCode.trim()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await getSpecificationFull({ item_code: itemCode.trim(), root_qty: rootQty, max_depth: 15 })
      setNodes(data.nodes ?? [])
      setMessage(`Загружено узлов: ${flatten(data.nodes ?? []).length}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Спецификация изделия</div>
        <div className="runBadge">Узлов: {rows.length}</div>
      </div>

      <DocumentWindow
        title="Спецификация изделия"
        subtitle="Диагностическое дерево состава изделия и операций"
        hotkeys="Загрузка по item_code / артикулу"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={rows.length ? 1 : 0}
            visibleTo={rows.length}
            total={rows.length}
            selectedCount={warningsCount}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <label className="inlineControl specCodeInput">
            <span>Артикул/код</span>
            <input value={itemCode} onChange={(e) => setItemCode(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && void load()} />
          </label>
          <label className="inlineControl">
            <span>Кол-во</span>
            <input type="number" min="0.001" step="1" value={rootQty} onChange={(e) => setRootQty(Number(e.target.value || 1))} />
          </label>
          <button className="primary" onClick={() => void load()} disabled={!itemCode.trim() || loading}>Загрузить</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="tablePane resultTablePane">
          <table className="journalTable specTable">
            <thead>
              <tr>
                <th>Наименование</th>
                <th>Артикул</th>
                <th>Этап</th>
                <th>Метод</th>
                <th>Кол-во</th>
                <th>Ед./Норма</th>
                <th>Σ Кол-во</th>
                <th>Проблемы</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={`${row.id}-${index}`} className={row.type === 'operation' ? 'operationRow' : ''}>
                  <td className="itemCell">
                    <strong style={{ paddingLeft: row.level * 18 }}>{row.type === 'operation' ? '⚙ ' : ''}{nodeTitle(row)}</strong>
                    <span>{row.type}</span>
                  </td>
                  <td>{row.article || ''}</td>
                  <td>{row.stage?.name || ''}</td>
                  <td>{row.type === 'operation' ? '' : row.replenishmentMethod || ''}</td>
                  <td className="numCell"><strong>{row.qtyPerParent == null ? '' : qty(row.qtyPerParent)}</strong></td>
                  <td>{row.type === 'operation' ? `${qty(row.timeNormNh)} н/ч` : row.unit || ''}</td>
                  <td className="numCell"><strong>{row.computed?.treeQty == null ? '' : qty(row.computed.treeQty)}</strong></td>
                  <td>{(row.warnings ?? []).join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DocumentWindow>
    </main>
  )
}
