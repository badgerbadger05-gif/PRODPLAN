import type { ProductionKind, ProductionResource, ProductionResourcePayload, ResourceProductionKind } from '../domain/resources'
import { api } from '../lib/api'

export function listResources() {
  return api<ProductionResource[]>('/v1/resources/')
}

export function createResource(payload: ProductionResourcePayload) {
  return api<ProductionResource>('/v1/resources/', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateResource(resourceId: number, payload: ProductionResourcePayload) {
  return api<ProductionResource>(`/v1/resources/${resourceId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function listResourceProductionKinds(resourceId: number) {
  return api<ResourceProductionKind[]>(`/v1/resources/${resourceId}/production-kinds`)
}

export function listProductionKinds() {
  return api<ProductionKind[]>('/v1/resources/production-kinds')
}

export function addResourceProductionKind(resourceId: number, productionKindId: number) {
  return api<ResourceProductionKind>(`/v1/resources/${resourceId}/production-kinds`, {
    method: 'POST',
    body: JSON.stringify({ resource_id: resourceId, production_kind_id: productionKindId }),
  })
}

export function removeResourceProductionKind(resourceId: number, productionKindId: number) {
  return api<{ status: string }>(`/v1/resources/${resourceId}/production-kinds/${productionKindId}`, {
    method: 'DELETE',
  })
}
