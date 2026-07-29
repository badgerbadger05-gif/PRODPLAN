import type {
  BomFlattenedItem,
  BomItem,
  SpecFlatRow,
  SpecNode,
} from '../../../domain/specification'

export function nodeTitle(node: SpecNode) {
  return node.type === 'operation'
    ? node.operation?.name || 'Операция'
    : node.name || 'Номенклатура'
}

export function flattenSpecNodes(
  nodes: SpecNode[],
  level = 0,
  path: string[] = [],
): SpecFlatRow[] {
  return nodes.flatMap((node) => {
    const title = nodeTitle(node)
    const nextPath = node.type === 'item' ? [...path, title] : path
    return [
      { ...node, level, path: nextPath },
      ...flattenSpecNodes(node.children ?? [], level + 1, nextPath),
    ]
  })
}

export function nodeItemId(node: SpecNode) {
  const payload = (node as SpecNode & { item?: { id?: number | string } }).item
  if (payload?.id == null) return null
  const parsed = Number(payload.id)
  return Number.isFinite(parsed) ? parsed : null
}

export function itemTitle(item?: BomItem | null) {
  if (!item) return ''
  return [item.item_article, item.item_name].filter(Boolean).join(' · ') || item.item_code
}

export function warningSeverity(warnings?: string[]) {
  if ((warnings ?? []).includes('CYCLE_DETECTED')) return 'failed'
  if ((warnings ?? []).length) return 'partial'
  return 'ready'
}

export function qualitySeverityClass(severity: string) {
  if (severity === 'error') return 'failed'
  if (severity === 'warning') return 'partial'
  return 'ready'
}

export function normalizeFilterValue(value?: string | null) {
  return String(value || '').trim().toLowerCase()
}

export function filterSpecRows(
  rows: SpecFlatRow[],
  query: string,
  replenishmentMethod: string,
) {
  const text = query.trim().toLowerCase()
  const method = normalizeFilterValue(replenishmentMethod)
  if (!text && !method) return rows
  return rows.filter((row) => {
    if (method && normalizeFilterValue(row.replenishmentMethod) !== method) return false
    const haystack = [
      nodeTitle(row),
      row.article,
      row.stage?.name,
      row.operation?.name,
      row.replenishmentMethod,
      ...(row.warnings ?? []),
    ].filter(Boolean).join(' ').toLowerCase()
    return !text || haystack.includes(text)
  })
}

export function getMethodOptions(
  rows: SpecFlatRow[],
  flattened: BomFlattenedItem[],
) {
  const methods = new Set<string>()
  rows.forEach((row) => {
    if (row.type === 'item' && row.replenishmentMethod) methods.add(row.replenishmentMethod)
  })
  flattened.forEach((row) => {
    if (row.replenishment_method) methods.add(row.replenishment_method)
  })
  return Array.from(methods).sort((a, b) => a.localeCompare(b, 'ru'))
}

export function filterFlattenedRows(
  rows: BomFlattenedItem[],
  replenishmentMethod: string,
) {
  const method = normalizeFilterValue(replenishmentMethod)
  if (!method) return rows
  return rows.filter(
    (row) => normalizeFilterValue(row.replenishment_method) === method,
  )
}
