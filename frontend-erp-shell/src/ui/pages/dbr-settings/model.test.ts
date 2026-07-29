import { describe, expect, it } from 'vitest'
import type { DbrCategoryRisk, DbrSettings } from '../../../domain/dbr'
import { normalizeCategoryRiskRows, toSettingsForm, toSettingsUpdate } from './model'

const settings: DbrSettings = {
  id: 1,
  frozen_days: 3,
  gate_horizon_workdays: 5,
  shelf_threshold_qty: '12.5',
  rt_machining_days: 2,
  rt_welding_days: 3,
  rt_painting_days: 4,
  batch_days_turning: 1,
  batch_days_bending: 2,
  batch_days_welding: 3,
  batch_days_paint_black: 4,
  batch_days_paint_color: 5,
  feeder_chain_enabled: true,
  feeder_load_horizon_weeks: 6,
  w2_warehouse_ref1c: undefined,
  w3_warehouse_ref1c: null,
  w4_warehouse_ref1c: 'W4',
  fastener_categories: ['Болты'],
}

describe('DBR settings model', () => {
  it('creates a stable editable form from nullable API settings', () => {
    expect(toSettingsForm(settings)).toEqual(expect.objectContaining({
      shelf_threshold_qty: 12.5,
      w2_warehouse_ref1c: '',
      w3_warehouse_ref1c: '',
      w4_warehouse_ref1c: 'W4',
      fastener_categories: ['Болты'],
    }))
  })

  it('normalizes settings values for the update contract', () => {
    const form = toSettingsForm(settings)

    expect(toSettingsUpdate({
      ...form,
      shelf_threshold_qty: '7.25',
      w2_warehouse_ref1c: ' W2-new ',
      w3_warehouse_ref1c: '   ',
      fastener_categories: [' Болты ', '', 'Гайки', 'Болты'],
    })).toEqual(expect.objectContaining({
      shelf_threshold_qty: 7.25,
      w2_warehouse_ref1c: 'W2-new',
      w3_warehouse_ref1c: null,
      fastener_categories: ['Болты', 'Гайки'],
    }))
  })

  it('filters blank risk rows and normalizes optional risk fields', () => {
    const rows: DbrCategoryRisk[] = [
      { id: 1, item_group: ' Электрика ', receipt_warehouse_ref1c: '  ', supply_risk_pct: '' },
      { id: 2, item_group: 'Подшипники', receipt_warehouse_ref1c: ' RECEIPT ', supply_risk_pct: '12.5' },
      { id: 0, item_group: '   ', receipt_warehouse_ref1c: 'IGNORED', supply_risk_pct: 99 },
    ]

    expect(normalizeCategoryRiskRows(rows)).toEqual([
      { item_group: 'Электрика', receipt_warehouse_ref1c: null, supply_risk_pct: null },
      { item_group: 'Подшипники', receipt_warehouse_ref1c: 'RECEIPT', supply_risk_pct: 12.5 },
    ])
  })
})
