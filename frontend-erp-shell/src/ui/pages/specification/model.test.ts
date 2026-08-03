import { describe, expect, it } from 'vitest'
import type {
  BomFlattenedItem,
  BomItemIdentity,
  SpecFlatRow,
  SpecNode,
} from '../../../domain/specification'
import {
  filterFlattenedRows,
  filterSpecRows,
  flattenSpecNodes,
  getMethodOptions,
  itemTitle,
  nodeItemId,
  nodeTitle,
  normalizeFilterValue,
  qualitySeverityClass,
  warningSeverity,
} from './model'

const component: SpecNode = {
  id: 'component',
  parentId: 'root',
  type: 'item',
  name: 'Подшипник',
  article: 'ПДШ-01',
  replenishmentMethod: 'Покупка',
  stage: { name: 'Сборка' },
  warnings: ['NO_SPEC'],
}

const operation: SpecNode = {
  id: 'operation',
  parentId: 'root',
  type: 'operation',
  operation: { name: 'Токарная обработка' },
  children: [component],
}

const root: SpecNode = {
  id: 'root',
  parentId: null,
  type: 'item',
  name: 'Насос',
  article: 'НАС-01',
  replenishmentMethod: 'Производство',
  children: [operation],
}

const flattened: BomFlattenedItem[] = [
  {
    item_id: 1,
    item_code: 'PUMP',
    name: 'Насос',
    replenishment_method: 'Производство',
    total_qty: 1,
    occurrences: 1,
    levels: [0],
    stages: [],
    paths: [],
    warnings: [],
  },
  {
    item_id: 2,
    item_code: 'BEARING',
    name: 'Подшипник',
    replenishment_method: 'Покупка',
    total_qty: 2,
    occurrences: 1,
    levels: [2],
    stages: ['Сборка'],
    paths: [],
    warnings: [],
  },
]

describe('specification model', () => {
  it('flattens depth-first and only item nodes extend the material path', () => {
    const rows = flattenSpecNodes([root])

    expect(rows.map(({ id, level, path }) => ({ id, level, path }))).toEqual([
      { id: 'root', level: 0, path: ['Насос'] },
      { id: 'operation', level: 1, path: ['Насос'] },
      { id: 'component', level: 2, path: ['Насос', 'Подшипник'] },
    ])
    expect(root).not.toHaveProperty('level')
  })

  it('keeps the established title and item-id fallbacks', () => {
    expect(nodeTitle(component)).toBe('Подшипник')
    expect(nodeTitle({ ...component, name: '' })).toBe('Номенклатура')
    expect(nodeTitle(operation)).toBe('Токарная обработка')
    expect(nodeTitle({ ...operation, operation: null })).toBe('Операция')

    expect(nodeItemId({ ...component, item: { id: '42' } } as SpecNode)).toBe(42)
    expect(nodeItemId({ ...component, item: { id: 'bad' } } as SpecNode)).toBeNull()
    expect(nodeItemId(component)).toBeNull()
  })

  it('formats item titles in article-name-code priority', () => {
    const item = {
      item_id: 1,
      item_code: 'PUMP',
      item_name: 'Насос',
      item_article: 'НАС-01',
    } satisfies BomItemIdentity

    expect(itemTitle(item)).toBe('НАС-01 · Насос')
    expect(itemTitle({ ...item, item_article: null })).toBe('Насос')
    expect(itemTitle({ ...item, item_article: null, item_name: '' })).toBe('PUMP')
    expect(itemTitle(null)).toBe('')
  })

  it('maps warning and quality severities to the existing pill classes', () => {
    expect(warningSeverity(['NO_SPEC', 'CYCLE_DETECTED'])).toBe('failed')
    expect(warningSeverity(['NO_SPEC'])).toBe('partial')
    expect(warningSeverity()).toBe('ready')
    expect(qualitySeverityClass('error')).toBe('failed')
    expect(qualitySeverityClass('warning')).toBe('partial')
    expect(qualitySeverityClass('info')).toBe('ready')
    expect(qualitySeverityClass('future-severity')).toBe('failed')
  })

  it('normalizes filters and combines exact method with case-insensitive text search', () => {
    const rows = flattenSpecNodes([root])

    expect(normalizeFilterValue('  ПоКуПкА ')).toBe('покупка')
    expect(filterSpecRows(rows, 'сборКА', ' покупка ')).toEqual([rows[2]])
    expect(filterSpecRows(rows, 'no_spec', '')).toEqual([rows[2]])
    expect(filterSpecRows(rows, 'токарная', '')).toEqual([rows[1]])
    expect(filterSpecRows(rows, '', '')).toBe(rows)
  })

  it('collects unique sorted methods from item tree rows and flattened rows', () => {
    const rows: SpecFlatRow[] = [
      ...flattenSpecNodes([root]),
      {
        id: 'operation-method',
        parentId: null,
        type: 'operation',
        level: 0,
        replenishmentMethod: 'Игнорировать',
      },
    ]

    expect(getMethodOptions(rows, [
      ...flattened,
      { ...flattened[0], item_id: 3, replenishment_method: 'Давальческое' },
    ])).toEqual(['Давальческое', 'Покупка', 'Производство'])
  })

  it('filters flattened rows by a normalized exact method', () => {
    expect(filterFlattenedRows(flattened, ' ПОКУПКА ')).toEqual([flattened[1]])
    expect(filterFlattenedRows(flattened, '')).toBe(flattened)
  })
})
