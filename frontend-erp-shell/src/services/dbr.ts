import type {
  DbrAssemblyRate,
  DbrAssemblyRateUpsert,
  DbrBoard,
  DbrBuildResult,
  DbrCategoryRisk,
  DbrCategoryRiskIn,
  DbrGateResult,
  DbrMoveResult,
  DbrProgram,
  DbrProgramCreate,
  DbrProgramUpdate,
  DbrReleaseResult,
  DbrRollForwardResult,
  DbrSchedule,
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

// ── Production programs (производственные программы) ───────────────────────────

export function listDbrPrograms(status?: string) {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return api<DbrProgram[]>(`/v1/dbr/programs${query}`)
}

export function getDbrProgram(programId: number) {
  return api<DbrProgram>(`/v1/dbr/programs/${programId}`)
}

export function createDbrProgram(payload: DbrProgramCreate) {
  return api<DbrProgram>('/v1/dbr/programs', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateDbrProgram(programId: number, payload: DbrProgramUpdate) {
  return api<DbrProgram>(`/v1/dbr/programs/${programId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function approveDbrProgram(programId: number) {
  return api<DbrProgram>(`/v1/dbr/programs/${programId}/approve`, {
    method: 'POST',
  })
}

// ── Drum schedule + board (барабан) ───────────────────────────────────────────

export function buildDbrDrum(programId: number) {
  return api<DbrBuildResult>('/v1/dbr/drum/build', {
    method: 'POST',
    body: JSON.stringify({ program_id: programId }),
  })
}

export function activateDbrDrum(scheduleId: number) {
  return api<DbrSchedule>(`/v1/dbr/drum/${scheduleId}/activate`, {
    method: 'POST',
  })
}

export function extendDbrDrum(scheduleId: number, programId: number) {
  return api<DbrBuildResult & { extended?: boolean; reason?: string }>(
    `/v1/dbr/drum/${scheduleId}/extend`,
    {
      method: 'POST',
      body: JSON.stringify({ program_id: programId }),
    },
  )
}

export function getDbrBoard(params?: { date_from?: string; date_to?: string }) {
  const search = new URLSearchParams()
  if (params?.date_from) search.set('date_from', params.date_from)
  if (params?.date_to) search.set('date_to', params.date_to)
  const query = search.toString()
  return api<DbrBoard>(`/v1/dbr/drum/active/board${query ? `?${query}` : ''}`)
}

export function refreshDbrGate(scheduleId: number) {
  return api<DbrGateResult>(`/v1/dbr/drum/${scheduleId}/refresh-gate`, {
    method: 'POST',
  })
}

export function rollForwardDbrDrum(scheduleId: number) {
  return api<DbrRollForwardResult>(`/v1/dbr/drum/${scheduleId}/roll-forward`, {
    method: 'POST',
  })
}

export function moveDbrSlot(slotId: number, newDate: string, newResourceId?: number) {
  return api<DbrMoveResult>(`/v1/dbr/drum/slots/${slotId}/move`, {
    method: 'POST',
    body: JSON.stringify({ new_date: newDate, new_resource_id: newResourceId ?? null }),
  })
}

export function releaseDbrSlot(slotId: number) {
  return api<DbrReleaseResult>(`/v1/dbr/drum/slots/${slotId}/release`, {
    method: 'POST',
  })
}
