import { describe, expect, it } from 'vitest'
import type { ResourceDistributionResult } from '../../../domain/stageDistribution'
import {
  aggregateComponents,
  flattenResourceComponents,
  type StageDistributionComponent,
} from './model'

const resource: ResourceDistributionResult = {
  resource_id: 1,
  resource_name: 'Механический участок',
  norm_hours: 7,
  products: [
    {
      root_item_id: 100,
      root_item_code: 'PUMP-01',
      root_item_name: 'Насос',
      components: [{
        item_id: 501,
        item_code: 'SHAFT-01',
        item_article: 'ВАЛ-01',
        item_name: 'Вал',
        qty_per_unit: 2,
        stock_qty: 6,
        norm_hours: 0.5,
        norm_hours_total: 2,
        stage_id: 8,
        stage_name: 'Токарная обработка',
      }],
    },
    {
      root_item_id: 101,
      root_item_code: 'GEAR-01',
      root_item_name: 'Редуктор',
      components: [
        {
          item_id: 501,
          item_code: 'SHAFT-01',
          item_article: 'ВАЛ-ДРУГОЙ',
          item_name: 'Вал из второй строки',
          qty_per_unit: 3,
          stock_qty: 99,
          norm_hours: 9,
          norm_hours_total: 3,
          stage_id: 8,
          stage_name: 'Другой label этапа',
        },
        {
          item_id: 501,
          item_code: 'SHAFT-01',
          item_name: 'Вал без этапа',
          qty_per_unit: 1,
          stock_qty: 4,
          norm_hours: 1,
          norm_hours_total: 1,
          stage_id: null,
          stage_name: null,
        },
      ],
    },
  ],
}

describe('stage distribution model', () => {
  it('flattens products in source order and annotates every component with its root', () => {
    const rows = flattenResourceComponents(resource)

    expect(rows.map(({ item_name, root }) => ({ item_name, root }))).toEqual([
      { item_name: 'Вал', root: 'Насос' },
      { item_name: 'Вал из второй строки', root: 'Редуктор' },
      { item_name: 'Вал без этапа', root: 'Редуктор' },
    ])
    expect(flattenResourceComponents(null)).toEqual([])
  })

  it('groups by item and nullable stage while preserving first-row metadata and order', () => {
    const rows = flattenResourceComponents(resource)

    expect(aggregateComponents(rows)).toEqual([
      {
        ...rows[0],
        qty_per_unit: 5,
        norm_hours_total: 5,
      },
      rows[2],
    ])
  })

  it('does not mutate source rows while aggregating numeric values', () => {
    const rows: StageDistributionComponent[] = flattenResourceComponents(resource)
    const snapshot = structuredClone(rows)

    const aggregated = aggregateComponents(rows)

    expect(rows).toEqual(snapshot)
    expect(aggregated[0]).not.toBe(rows[0])
  })
})
