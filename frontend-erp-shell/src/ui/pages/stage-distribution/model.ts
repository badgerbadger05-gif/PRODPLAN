import type {
  DistributedComponent,
  ResourceDistributionResult,
} from '../../../domain/stageDistribution'

export type StageDistributionComponent = DistributedComponent & {
  root: string
}

export function flattenResourceComponents(
  resource: ResourceDistributionResult | null | undefined,
): StageDistributionComponent[] {
  return (resource?.products ?? []).flatMap((product) => (
    product.components.map((component) => ({
      ...component,
      root: product.root_item_name,
    }))
  ))
}

export function aggregateComponents(
  rows: StageDistributionComponent[],
): StageDistributionComponent[] {
  const grouped = new Map<string, StageDistributionComponent>()
  rows.forEach((row) => {
    const key = `${row.item_id}:${row.stage_id ?? 'null'}`
    const existing = grouped.get(key)
    if (!existing) {
      grouped.set(key, { ...row })
      return
    }
    existing.qty_per_unit = Number(existing.qty_per_unit || 0) + Number(row.qty_per_unit || 0)
    existing.norm_hours_total = Number(existing.norm_hours_total || 0) + Number(row.norm_hours_total || 0)
  })
  return Array.from(grouped.values())
}
