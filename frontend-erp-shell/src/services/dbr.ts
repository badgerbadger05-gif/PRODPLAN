import type {
  DbrAssemblyRate,
  DbrAssemblyRateUpsert,
  DbrCategoryRisk,
  DbrCategoryRiskIn,
  DbrSettings,
  DbrSettingsUpdate,
} from '../domain/dbr'
import { api } from '../lib/api'

// ── Settings ────────────────────────────────────────────────────────────────

export function getDbrSettings() {
  return api<DbrSettings>('/v1/dbr/settings')
}

export function updateDbrSettings(patch: DbrSettingsUpdate) {
  return api<DbrSettings>('/v1/dbr/settings', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })
}

// ── Assembly rates (такты сборки) ─────────────────────────────────────────────

export function listDbrAssemblyRates() {
  return api<DbrAssemblyRate[]>('/v1/dbr/assembly-rates')
}

export function upsertDbrAssemblyRate(payload: DbrAssemblyRateUpsert) {
  return api<DbrAssemblyRate>('/v1/dbr/assembly-rates', {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function deleteDbrAssemblyRate(rateId: number) {
  return api<{ deleted: number }>(`/v1/dbr/assembly-rates/${rateId}`, {
    method: 'DELETE',
  })
}

// ── Category supply-risk (категорийные риски) ─────────────────────────────────

export function listDbrCategoryRisks() {
  return api<DbrCategoryRisk[]>('/v1/dbr/category-risks')
}

export function replaceDbrCategoryRisks(rows: DbrCategoryRiskIn[]) {
  return api<DbrCategoryRisk[]>('/v1/dbr/category-risks', {
    method: 'PUT',
    body: JSON.stringify({ rows }),
  })
}
