import type {
  ProductionKind,
  ProductionResource,
  ProductionResourcePayload,
  ResourceProductionKind,
} from '../../../domain/resources'

export function emptyResourceForm(): ProductionResourcePayload {
  return {
    resource_name: '',
    shift_offset: 0,
    planning_range: 30,
    capacity: 0,
    work_schedule: '5/2',
    daily_work_hours: 8,
    buffer_days: 0,
  }
}

export function resourceToForm(resource: ProductionResource): ProductionResourcePayload {
  return {
    resource_name: resource.resource_name || '',
    shift_offset: Number(resource.shift_offset ?? 0),
    planning_range: Number(resource.planning_range ?? 30),
    capacity: Number(resource.capacity ?? 0),
    work_schedule: resource.work_schedule || '5/2',
    daily_work_hours: Number(resource.daily_work_hours ?? 8),
    buffer_days: Number(resource.buffer_days ?? 0),
  }
}

export function normalizeResourcePayload(
  form: ProductionResourcePayload,
): ProductionResourcePayload {
  return {
    resource_name: String(form.resource_name || '').trim(),
    shift_offset: Number(form.shift_offset ?? 0),
    planning_range: Number(form.planning_range ?? 30),
    capacity: Number(form.capacity ?? 0),
    work_schedule: form.work_schedule || '5/2',
    daily_work_hours: Number(form.daily_work_hours ?? 8),
    buffer_days: Number(form.buffer_days ?? 0),
  }
}

export function productionKindName(kinds: ProductionKind[], id: number) {
  return kinds.find((kind) => kind.id === id)?.name || `ID ${id}`
}

export function availableKinds(
  kinds: ProductionKind[],
  assigned: ResourceProductionKind[],
) {
  const assignedIds = new Set(assigned.map((kind) => kind.production_kind_id))
  return kinds
    .filter((kind) => kind.id > 0 && !assignedIds.has(kind.id))
    .sort((a, b) => a.name.localeCompare(b.name, 'ru'))
}
