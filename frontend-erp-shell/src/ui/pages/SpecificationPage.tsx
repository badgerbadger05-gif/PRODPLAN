import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import type {
  BomFlattenedItem,
  BomItem,
  BomItemIdentity,
  BomQualityIssue,
  BomWhereUsedItem,
  SpecFlatRow,
  SpecNode,
} from '../../domain/specification'
import { downloadBase64File } from '../../lib/download'
import { qty } from '../../lib/format'
import {
  exportSpecificationXlsx,
  getSpecificationFlattened,
  getSpecificationFull,
  getSpecificationQuality,
  getSpecificationWhereUsed,
  searchSpecificationItems,
} from '../../services/specification'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { SpecRepairDialog, type RepairAction } from './specification/SpecRepairDialog'
import {
  SpecificationResults,
  type SpecificationTab,
} from './specification/SpecificationResults'
import {
  filterFlattenedRows,
  filterSpecRows,
  flattenSpecNodes,
  getMethodOptions,
  itemTitle,
  nodeItemId,
  nodeTitle,
} from './specification/model'

type LoadedBom = {
  item: BomItemIdentity
  nodes: SpecNode[]
  flattened: BomFlattenedItem[]
  whereUsed: BomWhereUsedItem[]
  quality: BomQualityIssue[]
}

export function SpecificationPage() {
  const [query, setQuery] = useState('')
  const [treeFilter, setTreeFilter] = useState('')
  const [methodFilter, setMethodFilter] = useState('')
  const [rootQty, setRootQty] = useState(1)
  const [tab, setTab] = useState<SpecificationTab>('tree')
  const [searchItems, setSearchItems] = useState<BomItem[]>([])
  const [loaded, setLoaded] = useState<LoadedBom | null>(null)
  const [selectedNode, setSelectedNode] = useState<SpecFlatRow | null>(null)
  const [selectedFlat, setSelectedFlat] = useState<BomFlattenedItem | null>(null)
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [repairAction, setRepairAction] = useState<RepairAction | null>(null)
  const [picking, setPicking] = useState(false)
  const loadSequence = useRef(0)
  const pickerRef = useRef<HTMLDivElement>(null)
  const pickerReturnFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!picking) return
    pickerReturnFocus.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    const selector = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    pickerRef.current?.querySelector<HTMLElement>(selector)?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setPicking(false)
        return
      }
      if (event.key !== 'Tab' || !pickerRef.current) return
      const focusable = [...pickerRef.current.querySelectorAll<HTMLElement>(selector)]
      if (!focusable.length) {
        event.preventDefault()
        pickerRef.current.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && (document.activeElement === first || !pickerRef.current.contains(document.activeElement))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !pickerRef.current.contains(document.activeElement))) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      pickerReturnFocus.current?.focus()
      pickerReturnFocus.current = null
    }
  }, [picking])

  const rows = useMemo(() => flattenSpecNodes(loaded?.nodes ?? []), [loaded])
  const filteredRows = useMemo(
    () => filterSpecRows(rows, treeFilter, methodFilter),
    [rows, treeFilter, methodFilter],
  )
  const methodOptions = useMemo(
    () => getMethodOptions(rows, loaded?.flattened ?? []),
    [rows, loaded?.flattened],
  )
  const filteredFlattened = useMemo(
    () => filterFlattenedRows(loaded?.flattened ?? [], methodFilter),
    [loaded?.flattened, methodFilter],
  )
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
      const items = data.items ?? []
      setSearchItems(items)
      if (items.length === 1) {
        await loadItem(items[0])
      } else if (items.length === 0) {
        setPicking(false)
        setMessage('Ничего не найдено')
      } else {
        // Несколько совпадений — показываем выбор (в т.ч. поверх уже загруженного изделия).
        setPicking(true)
        setMessage(`Найдено позиций: ${items.length}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function loadItem(item: BomItemIdentity) {
    const sequence = ++loadSequence.current
    setLoading(true)
    setError('')
    setMessage('')
    setPicking(false)
    try {
      const [tree, flattened, whereUsed, quality] = await Promise.all([
        getSpecificationFull({ item_id: item.item_id, root_qty: rootQty, max_depth: 20 }),
        getSpecificationFlattened({ item_id: item.item_id, root_qty: rootQty, max_depth: 20 }),
        getSpecificationWhereUsed({ item_id: item.item_id, max_depth: 10 }),
        getSpecificationQuality({ item_id: item.item_id, max_depth: 20 }),
      ])
      if (sequence !== loadSequence.current) return
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
      if (sequence !== loadSequence.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (sequence === loadSequence.current) setLoading(false)
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

  async function exportXlsx() {
    if (!loaded) return
    setExporting(true)
    setError('')
    try {
      const response = await exportSpecificationXlsx({
        item_id: loaded.item.item_id,
        root_qty: rootQty,
        max_depth: 20,
        replenishment_method: methodFilter || undefined,
      })
      downloadBase64File(response, `specification_${loaded.item.item_article || loaded.item.item_code}.xlsx`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setExporting(false)
    }
  }

  function handleTabKeyDown(
    event: ReactKeyboardEvent<HTMLSpanElement>,
    index: number,
  ) {
    const tabs: SpecificationTab[] = ['tree', 'flat', 'where-used', 'quality']
    let nextIndex = index
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = tabs.length - 1
    else return

    event.preventDefault()
    setTab(tabs[nextIndex])
    const tabList = event.currentTarget.parentElement
    ;(tabList?.children[nextIndex] as HTMLElement | undefined)?.focus()
  }

  const selectedTitle = selectedNode ? nodeTitle(selectedNode) : selectedFlat?.name ?? itemTitle(loaded?.item)
  const selectedAccessibleTitle = selectedNode
    ? selectedNode.type === 'item'
      ? [selectedNode.article, nodeTitle(selectedNode)].filter(Boolean).join(' · ')
      : nodeTitle(selectedNode)
    : selectedTitle

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
          <label className="inlineControl">
            <span>Метод</span>
            <select value={methodFilter} onChange={(e) => setMethodFilter(e.target.value)} disabled={!loaded}>
              <option value="">Все</option>
              {methodOptions.map((method) => (
                <option key={method} value={method}>{method}</option>
              ))}
            </select>
          </label>
          <div className="commandBarSpacer" />
          {loaded && <button onClick={() => void exportXlsx()} disabled={loading || exporting}>XLSX</button>}
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
                    </tr>
                  ))}
                  {!searchItems.length && (
                    <tr><td colSpan={4} className="emptyDetail">Введите артикул, код или часть названия и нажмите Найти</td></tr>
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

            <div className="tabsBar bomTabs" role="tablist">
              {([
                ['tree', 'Дерево'],
                ['flat', 'Плоская развертка'],
                ['where-used', 'Где используется'],
                ['quality', 'Качество'],
              ] as const).map(([value, label], index) => (
                <span
                  key={value}
                  id={`specification-tab-${value}`}
                  role="tab"
                  aria-controls={`specification-panel-${value}`}
                  aria-selected={tab === value}
                  tabIndex={tab === value ? 0 : -1}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                  style={{ display: 'contents' }}
                >
                  <button
                    className={tab === value ? 'activeTab' : ''}
                    onClick={() => setTab(value)}
                    tabIndex={-1}
                  >{label}</button>
                </span>
              ))}
            </div>

            <div className="split bomCockpitSplit">
              <SpecificationResults
                tab={tab}
                treeRows={filteredRows}
                selectedNode={selectedNode}
                onSelectNode={setSelectedNode}
                flattenedRows={filteredFlattened}
                selectedFlat={selectedFlat}
                onSelectFlat={setSelectedFlat}
                whereUsed={loaded.whereUsed}
                quality={loaded.quality}
              />

              <aside className="detailPane bomDetailPane">
                <h2 aria-label={selectedAccessibleTitle || 'BOM'}>{selectedTitle || 'BOM'}</h2>
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
                    onClick={() => setRepairAction('quantity')}
                    disabled={!selectedNode || selectedNode.type !== 'item' || selectedNode.componentId == null}
                    title="Изменить количество (норму расхода) этой строки состава"
                  >Изменить кол-во</button>
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

        {loaded && picking && searchItems.length > 0 && (
          <div className="dialogOverlay" onMouseDown={(e) => e.target === e.currentTarget && setPicking(false)}>
            <div
              ref={pickerRef}
              className="dialogBox bomPickerBox"
              role="dialog"
              aria-modal="true"
              aria-label={`Найдено позиций: ${searchItems.length} — выберите`}
              tabIndex={-1}
            >
              <div className="dialogHeader">Найдено позиций: {searchItems.length} — выберите</div>
              <div className="dialogBody">
                <table className="journalTable bomSearchTable">
                  <thead>
                    <tr><th>Номенклатура</th><th>Код</th><th>Спецификация</th><th>Метод</th></tr>
                  </thead>
                  <tbody>
                    {searchItems.map((item) => (
                      <tr key={item.item_id} onDoubleClick={() => void loadItem(item)}>
                        <td className="itemCell"><strong>{item.item_name}</strong><span>{item.item_article || ''}</span></td>
                        <td>{item.item_code}</td>
                        <td>
                          <button onClick={() => void loadItem(item)} disabled={loading || !item.spec_id}>Открыть</button>
                          <span className={`miniPill ${item.spec_id ? 'ready' : 'failed'}`}>{item.spec_id ? `#${item.spec_id}` : 'нет'}</span>
                        </td>
                        <td>{item.replenishment_method || ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="dialogFooter">
                <button onClick={() => setPicking(false)}>Закрыть</button>
              </div>
            </div>
          </div>
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
