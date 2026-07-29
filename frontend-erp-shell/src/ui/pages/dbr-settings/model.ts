import type {
  DbrCategoryRisk,
  DbrCategoryRiskIn,
  DbrSettings,
  DbrSettingsUpdate,
} from '../../../domain/dbr'

export type SettingsForm = Omit<DbrSettings, 'id'>

export function toSettingsForm(settings: DbrSettings): SettingsForm {
  return {
    frozen_days: settings.frozen_days,
    gate_horizon_workdays: settings.gate_horizon_workdays,
    shelf_threshold_qty: Number(settings.shelf_threshold_qty ?? 0),
    rt_machining_days: settings.rt_machining_days,
    rt_welding_days: settings.rt_welding_days,
    rt_painting_days: settings.rt_painting_days,
    batch_days_turning: settings.batch_days_turning,
    batch_days_bending: settings.batch_days_bending,
    batch_days_welding: settings.batch_days_welding,
    batch_days_paint_black: settings.batch_days_paint_black,
    batch_days_paint_color: settings.batch_days_paint_color,
    feeder_chain_enabled: settings.feeder_chain_enabled,
    feeder_load_horizon_weeks: settings.feeder_load_horizon_weeks,
    w2_warehouse_ref1c: settings.w2_warehouse_ref1c ?? '',
    w3_warehouse_ref1c: settings.w3_warehouse_ref1c ?? '',
    w4_warehouse_ref1c: settings.w4_warehouse_ref1c ?? '',
    fastener_categories: settings.fastener_categories ?? [],
  }
}

export function toSettingsUpdate(form: SettingsForm): DbrSettingsUpdate {
  return {
    ...form,
    shelf_threshold_qty: Number(form.shelf_threshold_qty ?? 0),
    w2_warehouse_ref1c: form.w2_warehouse_ref1c?.trim() || null,
    w3_warehouse_ref1c: form.w3_warehouse_ref1c?.trim() || null,
    w4_warehouse_ref1c: form.w4_warehouse_ref1c?.trim() || null,
    fastener_categories: Array.from(
      new Set(form.fastener_categories.map((name) => name.trim()).filter(Boolean)),
    ),
  }
}

export function normalizeCategoryRiskRows(rows: DbrCategoryRisk[]): DbrCategoryRiskIn[] {
  return rows
    .map((row) => ({
      item_group: String(row.item_group ?? '').trim(),
      receipt_warehouse_ref1c: (row.receipt_warehouse_ref1c ?? '').toString().trim() || null,
      supply_risk_pct:
        row.supply_risk_pct === '' || row.supply_risk_pct === null || row.supply_risk_pct === undefined
          ? null
          : Number(row.supply_risk_pct),
    }))
    .filter((row) => row.item_group)
}
