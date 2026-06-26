import { useMemo, useState, type CSSProperties } from 'react'
import type {
  BomFlattenedItem,
  BomItem,
  BomQualityIssue,
  BomWhereUsedItem,
  SpecFlatRow,
  SpecNode,
} from '../../domain/specification'
import { qty } from '../../lib/format'
import {
  getSpecificationFlattened,
  getSpecificationFull,
  getSpecificationQuality,
  getSpecificationWhereUsed,
  searchSpecificationItems,
} from '../../services/specification'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { SpecRepairDialog, type RepairAction } from './specification/SpecRepairDialog'

type BomTab = 'tree' | 'flat' | 'where-used' | 'quality'

type LoadedBom = {
  item: BomItem
  nodes: SpecNode[]
  flattened: BomFlattenedItem[]
  whereUsed: BomWhereUsedItem[]
  quality: BomQualityIssue[]
}

function flatten(nodes: SpecNode[], level = 0, path: string[] = []): SpecFlatRow[] {
  return nodes.flatMap((node) => {
    const title = nodeTitle(node)
    const nextPath = node.type === 'item' ? [...path, title] : path
    return [
      { ...node, level, path: nextPath },
      ...flatten(node.children ?? [], level + 1, nextPath),
    ]
  })
}

function nodeTitle(node: SpecNode) {
  return node.type === 'operation'
    ? node.operation?.name || 'Операция'
    : node.name || 'Номенклатура'
}

function nodeItemId(node: SpecNode) {
  const payload = (node as SpecNode & { item?: { id?: number | string } }).item
  if (payload?.id == null) return null
  const parsed = Number(payload.id)
  return Number.isFinite(parsed) ? parsed : null
}

function itemTitle(item?: BomItem | null) {
  if (!item) return ''
  return [item.item_article, item.item_name].filter(Boolean).join(' · ') || item.item_code
}

function warningSeverity(warnings?: string[]) {
  if ((warnings ?? []).includes('CYCLE_DETECTED')) return 'failed'
  if ((warnings ?? []).length) return 'partial'
  return 'ready'
}

function qualitySeverityClass(severity: string) {
  if (severity === 'error') return 'failed'
  if (severity === 'warning') return 'partial'
  return 'ready'
}

function useFilteredRows(rows: SpecFlatRow[], query: string) {
  return useMemo(() => {
    const text = query.trim().toLowerCase()
    if (!text) return rows
    return rows.filter((row) => {
      const haystack = [
        nodeTitle(row),
        row.article,
        row.stage?.name,
        row.operation?.name,
        row.replenishmentMethod,
        ...(row.warnings ?? []),
      ].filter(Boolean).join(' ').toLowerCase()
      return haystack.includes(text)
    })
  }, [rows, query])
}

export function SpecificationPage() {
  const [query, setQuery] = useState('')
  const [treeFilter, setTreeFilter] = useState('')
  const [rootQty, setRootQty] = useState(1)
  const [tab, setTab] = useState<BomTab>('tree')
  const [searchItems, setSearchItems] = useState<BomItem[]>([])
  const [loaded, setLoaded] = useState<LoadedBom | null>(null)
  const [selectedNode, setSelectedNode] = useState<SpecFlatRow | null>(null)
  const [selectedFlat, setSelectedFlat] = useState<BomFlattenedItem | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [repairAction, setRepairAction] = useState<RepairAction | null>(null)

  const rows = useMemo(() => flatten(loaded?.nodes ?? []), [loaded])
  const filteredRows = useFilteredRows(rows, treeFilter)
  const itemRows = rows.filter((row) => row.type === 'item')
  const operationRows = rows.filter((row) => row.type === 'operation')
  const warningsCount = rows.reduce((sum, row) => sum + (row.warnings?.length ?? 0), 0)
  const maxLevel = rows.reduce((max, row) => Math.max(max, row.level), 0)

  async function search() {
    if (!query.trim()) return
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await searchSpecificationItems({ q: query.trim(), limit: 60 })
      setSearchItems(data.items ?? [])
      if ((data.items ?? []).length === 1) {
        await loadItem(data.items[0])
      } else {
        setMessage(`Найдено позиций: ${(data.items ?? []).length}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function loadItem(item: BomItem) {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const [tree, flattened, whereUsed, quality] = await Promise.all([
        getSpecificationFull({ item_id: item.item_id, root_qty: rootQty, max_depth: 20 }),
        getSpecificationFlattened({ item_id: item.item_id, root_qty: rootQty, max_depth: 20 }),
        getSpecificationWhereUsed({ item_id: item.item_id, max_depth: 10 }),
        getSpecificationQuality({ item_id: item.item_id, max_depth: 20 }),
      ])
      setLoaded({
        item,
        nodes: tree.nodes ?? [],
        flattened: flattened.items ?? [],
        whereUsed: whereUsed.items ?? [],
        quality: quality.issues ?? [],
      })
      setSelectedNode(null)
      setSelectedFlat(null)
      setMessage(`Загружено: ${itemTitle(item)}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function openNodeAsRoot(row: SpecFlatRow) {
    const itemId = nodeItemId(row)
    if (!itemId) return
    await loadItem({
      item_id: itemId,
      item_code: String((row as SpecNode & { item?: { code?: string } }).item?.code ?? ''),
      item_name: String(row.name ?? ''),
      item_article: row.article,
      unit: row.unit,
      replenishment_method: row.replenishmentMethod,
      has_children: row.hasChildren,
    })
  }

  const selectedTitle = selectedNode ? nodeTitle(selectedNode) : selectedFlat?.name ?? itemTitle(loaded?.item)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Спецификации / BOM</div>
        <div className="runBadge">{loaded ? itemTitle(loaded.item) : 'Read-only'}</div>
      </div>

      <DocumentWindow
        title="BOM cockpit"
        subtitle="Поиск, дерево состава, плоская развертка, где используется и контроль качества"
        hotkeys="Enter — поиск / двойной переход через Открыть как корень"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={filteredRows.length ? 1 : 0}
            visibleTo={filteredRows.length}
            total={rows.length}
            selectedCount={warningsCount}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar bomCommandBar">
          <label className="inlineControl bomSearchInput">
            <span>Поиск</span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void search()}
              placeholder="Артикул, код, название"
            />
          </label>
          <label className="inlineControl">
            <span>Кол-во</span>
            <input type="number" min="0.001" step="1" value={rootQty} onChange={(e) => setRootQty(Number(e.target.value || 1))} />
          </label>
          <button className="primary" onClick={() => void search()} disabled={!query.trim() || loading}>Найти</button>
          <div className="barSeparator" />
          <label className="inlineControl bomSearchInput">
            <span>Фильтр дерева</span>
            <input value={treeFilter} onChange={(e) => setTreeFilter(e.target.value)} placeholder="Узел, этап, проблема" />
          </label>
          <div className="commandBarSpacer" />
          {loaded && <button onClick={() => void loadItem(loaded.item)} disabled={loading}>Обновить</button>}
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        {!loaded && (
          <div className="bomStart">
            <div className="tablePane bomSearchPane">
              <table className="journalTable bomSearchTable">
                <thead>
                  <tr>
                    <th>Номенклатура</th>
                    <th>Код</th>
                    <th>Спецификация</th>
                    <th>Метод</th>
                    <th>Остаток</th>
                  </tr>
                </thead>
                <tbody>
                  {searchItems.map((item) => (
                    <tr key={item.item_id} onDoubleClick={() => void loadItem(item)}>
                      <td className="itemCell">
                        <strong>{item.item_name}</strong>
                        <span>{item.item_article || ''}</span>
                      </td>
                      <td>{item.item_code}</td>
                      <td>
                        <button onClick={() => void loadItem(item)} disabled={loading || !item.spec_id}>Открыть</button>
                        <span className={`miniPill ${item.spec_id ? 'ready' : 'failed'}`}>{item.spec_id ? `#${item.spec_id}` : 'нет'}</span>
                      </td>
                      <td>{item.replenishment_method || ''}</td>
                      <td className="numCell"><strong>{qty(item.stock_qty)}</strong><span>{item.unit || ''}</span></td>
                    </tr>
                  ))}
                  {!searchItems.length && (
                    <tr><td colSpan={5} className="emptyDetail">Введите артикул, код или часть названия и нажмите Найти</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {loaded && (
          <>
            <div className="mrpSummaryStrip bomSummaryStrip">
              <div className="metricCell"><span>Узлы</span><strong>{rows.length}</strong><em>всего</em></div>
              <div className="metricCell"><span>Номенклатура</span><strong>{itemRows.length}</strong><em>строк</em></div>
              <div className="metricCell"><span>Операции</span><strong>{operationRows.length}</strong><em>строк</em></div>
              <div className="metricCell"><span>Глубина</span><strong>{maxLevel}</strong><em>уровней</em></div>
              <div className="metricCell"><span>Плоская</span><strong>{loaded.flattened.length}</strong><em>позиций</em></div>
              <div className="metricCell"><span>Проблемы</span><strong>{loaded.quality.length || warningsCount}</strong><em>quality</em></div>
            </div>

            <div className="tabsBar bomTabs">
              <button className={tab === 'tree' ? 'activeTab' : ''} onClick={() => setTab('tree')}>Дерево</button>
              <button className={tab === 'flat' ? 'activeTab' : ''} onClick={() => setTab('flat')}>Плоская развертка</button>
              <button className={tab === 'where-used' ? 'activeTab' : ''} onClick={() => setTab('where-used')}>Где используется</button>
              <button className={tab === 'quality' ? 'activeTab' : ''} onClick={() => setTab('quality')}>Качество</button>
            </div>

            <div className="split bomCockpitSplit">
              <div className="tablePane resultTablePane">
                {tab === 'tree' && (
                  <table className="journalTable bomTreeTable">
                    <thead>
                      <tr>
                        <th>Узел</th>
                        <th>Артикул</th>
                        <th>Этап</th>
                        <th>Метод</th>
                        <th>Кол-во</th>
                        <th>Ед./Норма</th>
                        <th>Итого</th>
                        <th>Проблемы</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredRows.map((row, index) => (
                        <tr
                          key={`${row.id}-${index}`}
                          className={`${row.type === 'operation' ? 'operationRow' : ''}${selectedNode?.id === row.id ? ' activeRow' : ''}`}
                          onClick={() => setSelectedNode(row)}
                        >
                          <td className="itemCell bomNameCell" style={{ '--level': row.level } as CSSProperties}>
                            <strong>{nodeTitle(row)}</strong>
                            <span>{row.type === 'operation' ? 'операция' : `уровень ${row.level}`}</span>
                          </td>
                          <td>{row.article || ''}</td>
                          <td>{row.stage?.name || ''}</td>
                          <td>{row.type === 'operation' ? '' : row.replenishmentMethod || ''}</td>
                          <td className="numCell"><strong>{row.qtyPerParent == null ? '' : qty(row.qtyPerParent)}</strong></td>
                          <td>{row.type === 'operation' ? `${qty(row.timeNormNh)} н/ч` : row.unit || ''}</td>
                          <td className="numCell"><strong>{row.computed?.treeQty == null ? '' : qty(row.computed.treeQty)}</strong></td>
                          <td><span className={`miniPill ${warningSeverity(row.warnings)}`}>{(row.warnings ?? []).length || 'ok'}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                {tab === 'flat' && (
                  <table className="journalTable bomFlatTable">
                    <thead>
                      <tr>
                        <th>Компонент</th>
                        <th>Артикул</th>
                        <th>Итого</th>
                        <th>Ед.</th>
                        <th>Вхождений</th>
                        <th>Уровни</th>
                        <th>Этапы</th>
                      </tr>
                    </thead>
                    <tbody>
                      {loaded.flattened.map((row) => (
                        <tr key={row.item_id} className={selectedFlat?.item_id === row.item_id ? 'activeRow' : ''} onClick={() => setSelectedFlat(row)}>
                          <td className="itemCell"><strong>{row.name}</strong><span>{row.item_code}</span></td>
                          <td>{row.article || ''}</td>
                          <td className="numCell"><strong>{qty(row.total_qty)}</strong></td>
                          <td>{row.unit || ''}</td>
                          <td className="numCell"><strong>{row.occurrences}</strong></td>
                          <td>{row.levels.join(', ')}</td>
                          <td>{row.stages.join(', ')}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                {tab === 'where-used' && (
                  <table className="journalTable bomWhereTable">
                    <colgroup>
                      <col className="bomWhereParentCol" />
                      <col className="bomWhereSpecCol" />
                      <col className="bomWhereSmallCol" />
                      <col className="bomWhereSmallCol" />
                      <col className="bomWhereQtyCol" />
                      <col className="bomWhereStageCol" />
                    </colgroup>
                    <thead>
                      <tr>
                        <th>Родитель</th>
                        <th>Спецификация</th>
                        <th>Уровень вверх</th>
                        <th>Кол-во</th>
                        <th>Итого к цели</th>
                        <th>Этап</th>
                      </tr>
                    </thead>
                    <tbody>
                      {loaded.whereUsed.map((row, index) => (
                        <tr key={`${row.parent.item_id}-${index}`}>
                          <td className="itemCell bomWhereParentCell">
                            <strong title={row.parent.item_name}>{row.parent.item_name}</strong>
                            <span title={row.parent.item_article || row.parent.item_code}>{row.parent.item_article || row.parent.item_code}</span>
                          </td>
                          <td className="bomWhereSpecCell" title={row.spec.spec_name || row.spec.spec_code || `#${row.spec.spec_id}`}>
                            {row.spec.spec_name || row.spec.spec_code || `#${row.spec.spec_id}`}
                          </td>
                          <td className="numCell"><strong>{row.level_up}</strong></td>
                          <td className="numCell"><strong>{qty(row.qty_per_parent)}</strong></td>
                          <td className="numCell"><strong>{qty(row.total_qty_to_target)}</strong></td>
                          <td className="bomWhereStageCell" title={row.stage?.name || ''}>{row.stage?.name || ''}</td>
                        </tr>
                      ))}
                      {!loaded.whereUsed.length && <tr><td colSpan={6} className="emptyDetail">В родительских спецификациях не найдено</td></tr>}
                    </tbody>
                  </table>
                )}

                {tab === 'quality' && (
                  <table className="journalTable bomQualityTable">
                    <thead>
                      <tr>
                        <th>Проблема</th>
                        <th>Серьезность</th>
                        <th>Номенклатура</th>
                        <th>Спецификация</th>
                      </tr>
                    </thead>
                    <tbody>
                      {loaded.quality.map((issue, index) => (
                        <tr key={`${issue.code}-${issue.spec_id ?? ''}-${issue.item?.item_id ?? ''}-${index}`}>
                          <td className="itemCell"><strong>{issue.message}</strong><span>{issue.code}</span></td>
                          <td><span className={`miniPill ${qualitySeverityClass(issue.severity)}`}>{issue.severity}</span></td>
                          <td>{issue.item ? [issue.item.item_article, issue.item.item_name].filter(Boolean).join(' · ') : ''}</td>
                          <td>{issue.spec_id ? `#${issue.spec_id}` : ''}</td>
                        </tr>
                      ))}
                      {!loaded.quality.length && <tr><td colSpan={4} className="emptyDetail">Критичных проблем не найдено</td></tr>}
                    </tbody>
                  </table>
                )}
              </div>

              <aside className="detailPane bomDetailPane">
                <h2>{selectedTitle || 'BOM'}</h2>
                <div className="detailMeta">
                  <span>{loaded.item.item_code}</span>
                  <span>{loaded.item.spec_id ? `Спецификация #${loaded.item.spec_id}` : 'Спецификация не найдена'}</span>
                </div>
                {selectedNode && (
                  <div className="detailGrid">
                    <span>Тип</span><strong>{selectedNode.type}</strong>
                    <span>Артикул</span><strong>{selectedNode.article || ''}</strong>
                    <span>Этап</span><strong>{selectedNode.stage?.name || ''}</strong>
                    <span>Метод</span><strong>{selectedNode.replenishmentMethod || ''}</strong>
                    <span>Итого</span><strong>{qty(selectedNode.computed?.treeQty)}</strong>
                    <span>Проблемы</span><strong>{(selectedNode.warnings ?? []).join(', ') || 'нет'}</strong>
                  </div>
                )}
                {selectedFlat && (
                  <div className="bomPathList">
                    <h3>Пути вхождения</h3>
                    {selectedFlat.paths.slice(0, 20).map((path, index) => (
                      <div key={`${path.path}-${index}`} className="bomPathItem">
                        <strong>{qty(path.qty)}</strong>
                        <span>{path.path}</span>
                      </div>
                    ))}
                  </div>
                )}
                <div className="detailActions">
                  <button onClick={() => selectedNode && void openNodeAsRoot(selectedNode)} disabled={!selectedNode || selectedNode.type !== 'item' || !nodeItemId(selectedNode)}>Открыть как корень</button>
                  <button onClick={() => setTab('where-used')}>Где используется</button>
                  <button onClick={() => setTab('quality')}>Качество</button>
                </div>
                <div className="detailActions detailRepairActions">
                  <button
                    onClick={() => setRepairAction('restage')}
                    disabled={!selectedNode || selectedNode.type !== 'item' || selectedNode.componentId == null}
                    title="Сменить этап списания этой строки состава"
                  >Сменить этап</button>
                  <button
                    onClick={() => setRepairAction('move')}
                    disabled={!selectedNode || selectedNode.type !== 'item' || selectedNode.componentId == null}
                    title="Перенести компонент в другую спецификацию"
                  >Перенести</button>
                  <button
                    onClick={() => setRepairAction('add')}
                    title="Добавить компонент в спецификацию выбранного узла"
                  >Добавить компонент</button>
                  <button
                    onClick={() => setRepairAction('remove')}
                    disabled={!selectedNode || selectedNode.type !== 'item' || selectedNode.componentId == null}
                    title="Убрать компонент из спецификации (с подтверждением)"
                  >Убрать компонент</button>
                  <button
                    onClick={() => setRepairAction('kind')}
                    disabled={!selectedNode || selectedNode.type !== 'item' || !nodeItemId(selectedNode)}
                    title="Превью каскада смены вида производства"
                  >Сменить вид произв.</button>
                </div>
              </aside>
            </div>
          </>
        )}

        {repairAction && loaded && (
          <SpecRepairDialog
            key={`${repairAction}-${selectedNode?.id ?? 'root'}`}
            action={repairAction}
            node={selectedNode}
            rootItem={loaded.item}
            treeRows={rows}
            onClose={() => setRepairAction(null)}
            onApplied={(msg) => {
              setRepairAction(null)
              setMessage(msg)
              void loadItem(loaded.item)
            }}
          />
        )}
      </DocumentWindow>
    </main>
  )
}
