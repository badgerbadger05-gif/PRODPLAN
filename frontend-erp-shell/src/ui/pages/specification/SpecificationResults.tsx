import type { CSSProperties, KeyboardEvent } from 'react'
import type {
  BomFlattenedItem,
  BomQualityIssue,
  BomWhereUsedItem,
  SpecFlatRow,
} from '../../../domain/specification'
import { qty } from '../../../lib/format'
import { nodeTitle, qualitySeverityClass, warningSeverity } from './model'

export type SpecificationTab = 'tree' | 'flat' | 'where-used' | 'quality'

type SpecificationResultsProps = {
  tab: SpecificationTab
  treeRows: SpecFlatRow[]
  selectedNode: SpecFlatRow | null
  onSelectNode: (row: SpecFlatRow) => void
  flattenedRows: BomFlattenedItem[]
  selectedFlat: BomFlattenedItem | null
  onSelectFlat: (row: BomFlattenedItem) => void
  whereUsed: BomWhereUsedItem[]
  quality: BomQualityIssue[]
}

export function SpecificationResults({
  tab,
  treeRows,
  selectedNode,
  onSelectNode,
  flattenedRows,
  selectedFlat,
  onSelectFlat,
  whereUsed,
  quality,
}: SpecificationResultsProps) {
  const panelId = `specification-panel-${tab}`
  const selectedTreeIndex = selectedNode
    ? treeRows.findIndex((row) => row.id === selectedNode.id)
    : treeRows.length ? 0 : -1

  function handleTreeKeyDown(
    event: KeyboardEvent<HTMLTableRowElement>,
    index: number,
  ) {
    let nextIndex = index
    if (event.key === 'ArrowDown') nextIndex = Math.min(index + 1, treeRows.length - 1)
    else if (event.key === 'ArrowUp') nextIndex = Math.max(index - 1, 0)
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = treeRows.length - 1
    else if (event.key !== 'Enter' && event.key !== ' ') return

    event.preventDefault()
    const nextRow = treeRows[nextIndex]
    if (!nextRow) return
    onSelectNode(nextRow)
    const body = event.currentTarget.parentElement
    ;(body?.children[nextIndex] as HTMLElement | undefined)?.focus()
  }

  return (
    <>
    <div
      className="tablePane resultTablePane"
      id={panelId}
      role="tabpanel"
      aria-labelledby={`specification-tab-${tab}`}
    >
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
            {treeRows.map((row, index) => (
              <tr
                key={`${row.id}-${index}`}
                className={`${row.type === 'operation' ? 'operationRow' : ''}${selectedNode?.id === row.id ? ' activeRow' : ''}`}
                onClick={() => onSelectNode(row)}
                onKeyDown={(event) => handleTreeKeyDown(event, index)}
                tabIndex={selectedTreeIndex === index ? 0 : -1}
                aria-selected={selectedTreeIndex === index}
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
              <th>Метод</th>
              <th>Итого</th>
              <th>Ед.</th>
              <th>Вхождений</th>
              <th>Уровни</th>
              <th>Этапы</th>
            </tr>
          </thead>
          <tbody>
            {flattenedRows.map((row) => (
              <tr key={row.item_id} className={selectedFlat?.item_id === row.item_id ? 'activeRow' : ''} onClick={() => onSelectFlat(row)}>
                <td className="itemCell"><strong>{row.name}</strong><span>{row.item_code}</span></td>
                <td>{row.article || ''}</td>
                <td>{row.replenishment_method || ''}</td>
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
            {whereUsed.map((row, index) => (
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
            {!whereUsed.length && <tr><td colSpan={6} className="emptyDetail">В родительских спецификациях не найдено</td></tr>}
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
            {quality.map((issue, index) => (
              <tr key={`${issue.code}-${issue.spec_id ?? ''}-${issue.item?.item_id ?? ''}-${index}`}>
                <td className="itemCell"><strong>{issue.message}</strong><span>{issue.code}</span></td>
                <td><span className={`miniPill ${qualitySeverityClass(issue.severity)}`}>{issue.severity}</span></td>
                <td>{issue.item ? [issue.item.item_article, issue.item.item_name].filter(Boolean).join(' · ') : ''}</td>
                <td>{issue.spec_id ? `#${issue.spec_id}` : ''}</td>
              </tr>
            ))}
            {!quality.length && <tr><td colSpan={4} className="emptyDetail">Критичных проблем не найдено</td></tr>}
          </tbody>
        </table>
      )}
    </div>
    {(['tree', 'flat', 'where-used', 'quality'] as const)
      .filter((candidate) => candidate !== tab)
      .map((candidate) => (
        <div
          key={candidate}
          id={`specification-panel-${candidate}`}
          role="tabpanel"
          aria-labelledby={`specification-tab-${candidate}`}
          hidden
        />
      ))}
    </>
  )
}
