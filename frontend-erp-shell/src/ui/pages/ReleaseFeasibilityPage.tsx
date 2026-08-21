import { useMemo, useState, type CSSProperties } from 'react'
import type {
  FeasibilityItem,
  FeasibilityResponse,
  FeasibilityRow,
  FeasibilityStatus,
  FeasibilityTreeNode,
} from '../../domain/releaseFeasibility'
import { qty } from '../../lib/format'
import { analyzeRelease, searchReleaseItems } from '../../services/releaseFeasibility'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const STATUS_TITLE: Record<FeasibilityStatus, string> = {
  ok: 'хватает',
  make: 'нет узла',
  shortage: 'нет материала',
  blocked: 'нельзя изготовить',
}

const STATUS_HINT: Record<FeasibilityStatus, string> = {
  ok: 'Остатка хватает на всю потребность',
  make: 'Самого узла на складе нет, но всех его компонентов хватает — узел надо изготовить',
  shortage: 'Материал/покупное нечем закрыть — это и есть блокировка выпуска',
  blocked: 'Узел изготовить нельзя: внутри него не хватает компонентов',
}

function statusPill(status: FeasibilityStatus) {
  if (status === 'shortage' || status === 'blocked') return 'shortage'
  if (status === 'make') return 'partial'
  return 'ready'
}

function statusRowClass(status: FeasibilityStatus) {
  if (status === 'shortage' || status === 'blocked') return 'rowShortage'
  if (status === 'make') return 'rowMake'
  return ''
}

function summaryTitle(result: FeasibilityResponse) {
  if (!result.root.has_spec) return 'Нет спецификации'
  const { summary } = result
  if (summary.status === 'blocked') return 'Выпуск заблокирован'
  if (summary.status === 'make') return 'Нужно изготовить узлы'
  return 'Выпуск обеспечен'
}

function itemTitle(item: { item_article?: string | null; item_name?: string | null; item_code?: string | null }) {
  return [item.item_article, item.item_name].filter(Boolean).join(' · ') || String(item.item_code || '')
}

function flattenTree(node: FeasibilityTreeNode | null): FeasibilityTreeNode[] {
  if (!node) return []
  return [node, ...(node.children ?? []).flatMap((child) => flattenTree(child))]
}

export function ReleaseFeasibilityPage() {
  const [article, setArticle] = useState('')
  const [qtyInput, setQtyInput] = useState('1')
  const [candidates, setCandidates] = useState<FeasibilityItem[]>([])
  const [result, setResult] = useState<FeasibilityResponse | null>(null)
  const [treeMode, setTreeMode] = useState(false)
  const [criticalOnly, setCriticalOnly] = useState(false)
  const [selected, setSelected] = useState<FeasibilityRow | null>(null)
  const [loading, setLoading] = useState(false)
  const [expanding, setExpanding] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const requestedQty = Number(String(qtyInput).replace(',', '.')) || 0

  const blockingRows = useMemo(() => {
    const rows = result?.blocking ?? []
    return criticalOnly ? rows.filter((row) => row.is_blocking) : rows
  }, [result, criticalOnly])

  const treeRows = useMemo(() => flattenTree(result?.tree ?? null), [result])
  const visibleTreeRows = useMemo(
    () => (criticalOnly ? treeRows.filter((row) => row.status === 'shortage' || row.status === 'blocked') : treeRows),
    [treeRows, criticalOnly],
  )

  const shownCount = treeMode ? visibleTreeRows.length : blockingRows.length
  const totalCount = treeMode ? treeRows.length : (result?.blocking.length ?? 0)

  async function check() {
    const term = article.trim()
    if (!term) return
    if (!(requestedQty > 0)) {
      setError('Укажите количество к выпуску больше нуля')
      return
    }
    setLoading(true)
    setError('')
    setMessage('')
    setCandidates([])
    try {
      const found = await searchReleaseItems({ q: term, limit: 60 })
      const items = found.items ?? []
      if (!items.length) {
        setMessage('Ничего не найдено')
        return
      }
      const exact = items.filter(
        (item) => item.item_article.toLowerCase() === term.toLowerCase() || item.item_code.toLowerCase() === term.toLowerCase(),
      )
      const picked = exact.length === 1 ? exact[0] : items.length === 1 ? items[0] : null
      if (!picked) {
        setCandidates(items)
        setMessage(`Найдено позиций: ${items.length} — выберите изделие`)
        return
      }
      await runAnalyze(picked.item_id, false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function runAnalyze(itemId: number, includeTree: boolean) {
    setError('')
    setCandidates([])
    const data = await analyzeRelease({ item_id: itemId, qty: requestedQty, include_tree: includeTree })
    setResult(data)
    setTreeMode(includeTree)
    setSelected(null)
    setMessage(`${itemTitle(data.root)} — ${qty(data.root.requested_qty)} ${data.root.unit || ''}`.trim())
  }

  async function pickCandidate(item: FeasibilityItem) {
    setLoading(true)
    try {
      await runAnalyze(item.item_id, false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function toggleTree() {
    if (!result) return
    if (treeMode) {
      setTreeMode(false)
      return
    }
    if (result.tree) {
      setTreeMode(true)
      return
    }
    setExpanding(true)
    setError('')
    try {
      const data = await analyzeRelease({ item_id: result.root.item_id, qty: result.root.requested_qty, include_tree: true })
      setResult(data)
      setTreeMode(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExpanding(false)
    }
  }

  async function refresh() {
    if (!result) return
    setLoading(true)
    try {
      await runAnalyze(result.root.item_id, treeMode)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Проверка выпуска</div>
        <div className="runBadge">{result ? summaryTitle(result) : 'Read-only'}</div>
      </div>

      <DocumentWindow
        title="Проверка выпуска"
        subtitle="Что мешает выпустить изделие в заданном количестве: блокирующие узлы и материалы по остаткам складов"
        hotkeys="Enter — проверить / Развернуть BOM — полный состав"
        footer={(
          <StatusBar
            loading={loading || expanding}
            visibleFrom={shownCount ? 1 : 0}
            visibleTo={shownCount}
            total={totalCount}
            selectedCount={result?.summary.shortage_count ?? 0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <label className="inlineControl feasibilityArticleInput">
            <span>Артикул изделия</span>
            <input
              value={article}
              onChange={(e) => setArticle(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void check()}
              placeholder="Артикул, код или часть названия"
            />
          </label>
          <label className="inlineControl">
            <span>Количество</span>
            <input
              type="number"
              min="0.001"
              step="1"
              value={qtyInput}
              onChange={(e) => setQtyInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void check()}
            />
          </label>
          <button className="primary" onClick={() => void check()} disabled={!article.trim() || loading}>Проверить</button>
          <div className="barSeparator" />
          <button onClick={() => void toggleTree()} disabled={!result || !result.root.has_spec || loading || expanding}>
            {treeMode ? 'Только блокирующие' : 'Развернуть весь BOM'}
          </button>
          <label className="inlineControl">
            <input type="checkbox" checked={criticalOnly} onChange={(e) => setCriticalOnly(e.target.checked)} />
            <span>Только красные</span>
          </label>
          <div className="commandBarSpacer" />
          {result && <button onClick={() => void refresh()} disabled={loading || expanding}>Обновить</button>}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && !error && <div className="successLine">{message}</div>}
        {result && !result.root.has_spec && (
          <div className="errorLine">У изделия нет основной спецификации — состав развернуть нельзя</div>
        )}
        {result?.summary.warnings.includes('CYCLE_DETECTED') && (
          <div className="warningLine">В составе найден цикл — ветка обрезана: {result.summary.cycles.join(', ')}</div>
        )}
        {result?.summary.warnings.includes('DEPTH_LIMIT_REACHED') && (
          <div className="warningLine">Достигнут предел глубины разворота ({result.summary.max_depth} уровней)</div>
        )}

        {candidates.length > 0 && (
          <div className="tablePane feasibilityPickPane">
            <table className="journalTable">
              <thead>
                <tr>
                  <th>Изделие</th>
                  <th>Код</th>
                  <th className="numCell">Остаток</th>
                  <th>Спецификация</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((item) => (
                  <tr key={item.item_id} onDoubleClick={() => void pickCandidate(item)}>
                    <td className="itemCell">
                      <strong>{item.item_name}</strong>
                      <span>{item.item_article}</span>
                    </td>
                    <td>{item.item_code}</td>
                    <td className="numCell"><strong>{qty(item.stock_on_hand)}</strong><span>{item.unit || ''}</span></td>
                    <td>
                      <button onClick={() => void pickCandidate(item)} disabled={loading}>Проверить</button>
                      <span className={`miniPill ${item.has_spec ? 'ready' : 'failed'}`}>{item.has_spec ? 'есть' : 'нет'}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {result && candidates.length === 0 && (
          <>
            <div className="mrpSummaryStrip">
              <div className="metricCell">
                <span>Изделие</span>
                <strong>{qty(result.root.requested_qty)}</strong>
                <em>{result.root.item_article || result.root.item_code}</em>
              </div>
              <div className="metricCell">
                <span>Можно выпустить</span>
                <strong className={result.summary.fully_producible ? '' : 'metricAlert'}>{qty(result.summary.producible_qty)}</strong>
                <em>{result.root.unit || 'шт'} по остаткам</em>
              </div>
              <div className="metricCell">
                <span>Нет материалов</span>
                <strong className={result.summary.shortage_count ? 'metricAlert' : ''}>{result.summary.shortage_count}</strong>
                <em>красные</em>
              </div>
              <div className="metricCell">
                <span>Узлов заблокировано</span>
                <strong className={result.summary.blocked_count ? 'metricAlert' : ''}>{result.summary.blocked_count}</strong>
                <em>красные</em>
              </div>
              <div className="metricCell">
                <span>Изготовить узлов</span>
                <strong>{result.summary.make_count}</strong>
                <em>жёлтые</em>
              </div>
              <div className="metricCell">
                <span>Проверено</span>
                <strong>{result.summary.items_checked}</strong>
                <em>позиций, {result.summary.max_level} ур.</em>
              </div>
            </div>

            <div className="split">
              <div className="tablePane">
                {!treeMode && (
                  <table className="journalTable feasibilityTable">
                    <thead>
                      <tr>
                        <th>Позиция</th>
                        <th className="feasKindCell">Тип</th>
                        <th className="numCell">Требуется</th>
                        <th className="numCell">На складах</th>
                        <th className="numCell">Не хватает</th>
                        <th className="feasUnitCell">Ед.</th>
                        <th className="feasReasonCell">Причина</th>
                        <th className="feasRtCell">RT, дн</th>
                        <th className="feasStatusCell">Статус</th>
                      </tr>
                    </thead>
                    <tbody>
                      {blockingRows.map((row) => (
                        <tr
                          key={row.item_id}
                          className={`${statusRowClass(row.status)}${selected?.item_id === row.item_id ? ' activeRow' : ''}`}
                          onClick={() => setSelected(row)}
                          title={STATUS_HINT[row.status]}
                        >
                          <td className="itemCell">
                            <strong>{row.item_name}</strong>
                            <span>{row.item_article || row.item_code}</span>
                          </td>
                          <td className="feasKindCell">{row.kind === 'node' ? 'узел' : 'материал'}</td>
                          <td className="numCell">
                            <strong>{row.needed_now ? qty(row.required_qty) : '—'}</strong>
                          </td>
                          <td className="numCell"><strong>{qty(row.stock_on_hand)}</strong></td>
                          <td className="numCell"><strong>{qty(row.shortage_qty)}</strong></td>
                          <td className="feasUnitCell">{row.unit || ''}</td>
                          <td className="feasReasonCell">{row.reason}</td>
                          <td className="feasRtCell numCell">
                            {row.replenishment_time == null ? '' : row.replenishment_time}
                          </td>
                          <td className="feasStatusCell">
                            <span className={`miniPill ${statusPill(row.status)}`}>{STATUS_TITLE[row.status]}</span>
                          </td>
                        </tr>
                      ))}
                      {!blockingRows.length && (
                        <tr>
                          <td colSpan={9} className="emptyDetail">
                            {result.root.has_spec
                              ? 'Блокирующих позиций нет — количества хватает на весь выпуск'
                              : 'Состав не развёрнут: у изделия нет спецификации'}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                )}

                {treeMode && (
                  <table className="journalTable feasibilityTable feasibilityTreeTable">
                    <thead>
                      <tr>
                        <th>Состав</th>
                        <th className="numCell">На единицу</th>
                        <th className="numCell">Нужно по ветке</th>
                        <th className="numCell">На складах</th>
                        <th className="numCell">Не хватает</th>
                        <th className="feasUnitCell">Ед.</th>
                        <th className="feasReasonCell">Причина</th>
                        <th className="feasRtCell">RT, дн</th>
                        <th className="feasStatusCell">Статус</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleTreeRows.map((row) => (
                        <tr
                          key={row.key}
                          className={statusRowClass(row.status)}
                          title={STATUS_HINT[row.status]}
                        >
                          <td className="itemCell bomNameCell" style={{ '--level': row.level } as CSSProperties}>
                            <strong>{row.item_name}</strong>
                            <span>{row.item_article || row.item_code}</span>
                          </td>
                          <td className="numCell"><strong>{row.qty_per_parent == null ? '' : qty(row.qty_per_parent)}</strong></td>
                          <td className="numCell"><strong>{qty(row.branch_required_qty)}</strong></td>
                          <td className={`numCell${row.stock_short ? ' feasStockShort' : ''}`}>
                            <strong>{qty(row.stock_on_hand)}</strong>
                          </td>
                          <td className="numCell"><strong>{qty(row.shortage_qty)}</strong></td>
                          <td className="feasUnitCell">{row.unit || ''}</td>
                          <td className="feasReasonCell">{row.reason}</td>
                          <td className="feasRtCell numCell">
                            {row.replenishment_time == null ? '' : row.replenishment_time}
                          </td>
                          <td className="feasStatusCell">
                            <span className={`miniPill ${statusPill(row.status)}`}>{STATUS_TITLE[row.status]}</span>
                          </td>
                        </tr>
                      ))}
                      {!visibleTreeRows.length && (
                        <tr><td colSpan={9} className="emptyDetail">Нечего показать</td></tr>
                      )}
                    </tbody>
                  </table>
                )}
                {treeMode && result.tree_truncated && (
                  <div className="warningLine">Дерево показано частично: сработало ограничение глубины или размера</div>
                )}
              </div>

              <aside className="detailPane">
                {!selected && (
                  <>
                    <h2>{summaryTitle(result)}</h2>
                    <div className="detailTitle">{itemTitle(result.root)}</div>
                    <div className="detailMeta">
                      {treeMode ? 'Показан весь состав' : 'Показаны только мешающие позиции'}
                    </div>
                    <div className="detailGrid">
                      <span>К выпуску</span><strong>{qty(result.root.requested_qty)} {result.root.unit || ''}</strong>
                      <span>Можно сейчас</span><strong>{qty(result.summary.producible_qty)} {result.root.unit || ''}</strong>
                      <span>Готовых на складах</span><strong>{qty(result.root.stock_on_hand)} {result.root.unit || ''}</strong>
                      <span>Глубина</span><strong>{result.summary.max_level}</strong>
                    </div>
                    <p className="dialogHint">
                      Красным помечены позиции, которые нечем закрыть, и узлы, которые из-за них нельзя изготовить.
                      Жёлтым — узлы, которых нет на складе, но все компоненты для них есть.
                    </p>
                    <p className="dialogHint">
                      Готовые изделия на складе задание на выпуск не уменьшают. Остатки — свободные, с учётом настроек складов;
                      открытые заказы и поставки в пути не учитываются.
                    </p>
                  </>
                )}

                {selected && (
                  <>
                    <h2>{STATUS_TITLE[selected.status]}</h2>
                    <div className="detailTitle">{itemTitle(selected)}</div>
                    <div className="detailMeta">{STATUS_HINT[selected.status]}</div>
                    <div className="detailGrid">
                      <span>Тип</span><strong>{selected.kind === 'node' ? 'узел (есть состав)' : 'материал / покупное'}</strong>
                      <span>Требуется</span><strong>{qty(selected.required_qty)} {selected.unit || ''}</strong>
                      <span>На складах</span><strong>{qty(selected.stock_on_hand)} {selected.unit || ''}</strong>
                      <span>Пойдёт в дело</span><strong>{qty(selected.allocated_qty)} {selected.unit || ''}</strong>
                      <span>Не хватает</span><strong>{qty(selected.shortage_qty)} {selected.unit || ''}</strong>
                      <span>Пополнение</span><strong>{selected.replenishment_method || '—'}</strong>
                      <span>Код</span><strong>{selected.item_code}</strong>
                    </div>

                    <h3>Остатки по складам</h3>
                    <div className="detailList">
                      {selected.warehouses.map((warehouse) => (
                        <div className="detailListRow" key={`${warehouse.warehouse_name}-${warehouse.qty}`}>
                          <span>{warehouse.warehouse_name}</span>
                          <strong>{qty(warehouse.qty)}</strong>
                          {!warehouse.counted && <span className="miniPill failed">не в расчёте</span>}
                        </div>
                      ))}
                      {!selected.warehouses.length && (
                        <div className="detailListRow"><span>Разбивки по складам нет</span></div>
                      )}
                    </div>

                    <h3>Где применяется</h3>
                    <div className="detailList">
                      {selected.used_in.map((parent) => (
                        <div className="detailListRow" key={parent.item_id}>
                          <span>{parent.item_article || ''}</span>
                          <strong>{parent.item_name}</strong>
                        </div>
                      ))}
                      {!selected.used_in.length && (
                        <div className="detailListRow"><span>Входит напрямую в изделие</span></div>
                      )}
                    </div>

                    <div className="detailActions">
                      <button onClick={() => setSelected(null)}>Назад к итогам</button>
                    </div>
                  </>
                )}
              </aside>
            </div>
          </>
        )}

        {!result && candidates.length === 0 && (
          <div className="emptyDetail feasibilityStart">
            Введите артикул изделия и количество к выпуску — страница покажет узлы и материалы, которых не хватает.
          </div>
        )}
      </DocumentWindow>
    </main>
  )
}
