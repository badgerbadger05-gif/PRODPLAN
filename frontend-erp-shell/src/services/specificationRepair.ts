import type {
  AddRequest,
  AddResult,
  KindChangePreviewRequest,
  KindChangePreviewResult,
  MoveRequest,
  MoveResult,
  ProductionKind,
  RemoveRequest,
  RemoveResult,
  RestageRequest,
  RestageResult,
  SetQuantityRequest,
  SetQuantityResult,
  StageOption,
} from '../domain/specificationRepair'
import { api } from '../lib/api'

const BASE = '/v1/specification-repair'

export function repairRestage(req: RestageRequest) {
  return api<RestageResult>(`${BASE}/restage`, { method: 'POST', body: JSON.stringify(req) })
}

export function repairMove(req: MoveRequest) {
  return api<MoveResult>(`${BASE}/move`, { method: 'POST', body: JSON.stringify(req) })
}

export function repairAdd(req: AddRequest) {
  return api<AddResult>(`${BASE}/add`, { method: 'POST', body: JSON.stringify(req) })
}

export function repairRemove(req: RemoveRequest) {
  return api<RemoveResult>(`${BASE}/remove`, { method: 'POST', body: JSON.stringify(req) })
}

export function repairSetQuantity(req: SetQuantityRequest) {
  return api<SetQuantityResult>(`${BASE}/set-quantity`, { method: 'POST', body: JSON.stringify(req) })
}

export function repairKindChangePreview(req: KindChangePreviewRequest) {
  return api<KindChangePreviewResult>(`${BASE}/kind-change/preview`, {
    method: 'POST',
    body: JSON.stringify(req),
  })
}

// Справочники для выпадающих списков ремонтных диалогов.
export function listStages() {
  return api<StageOption[]>('/v1/plan/stages')
}

export function listProductionKinds() {
  return api<ProductionKind[]>('/v1/resources/production-kinds')
}
