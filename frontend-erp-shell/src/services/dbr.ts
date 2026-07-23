import type {
  DbrAssemblyRate,
  DbrAssemblyRateUpsert,
  DbrBoard,
  DbrBuildResult,
  DbrCategoryRisk,
  DbrCategoryRiskIn,
  DbrChainPreview,
  DbrChainRefresh,
  DbrFeederDeficitsResult,
  DbrFeederCockpit,
  DbrProcessingBoard,
  DbrProcessingChainPreview,
  DbrProcessingOrderPreview,
  DbrProcessingTripManifest,
  DbrGateResult,
  DbrFeederFilters,
  DbrFeederPosition,
  DbrFeederPreview,
  DbrFeederSignal,
  DbrFeederSignalFilters,
  DbrFeederSignalPreview,
  DbrMoveResult,
  DbrProgram,
  DbrProgramCreate,
  DbrProgramUpdate,
  DbrPurchaseLaunchResult,
  DbrPurchaseCockpit,
  DbrPurchasePlanPreview,
  DbrReleaseDayResult,
  DbrReleaseResult,
  DbrRollForwardResult,
  DbrSchedule,
  DbrSignalLaunchResult,
  DbrSettings,
  DbrSettingsUpdate,
} from '../domain/dbr'
import { api, apiText, ApiError } from '../lib/api'

export function isDbrConflict(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409
}

export function dbrSnapshotUnavailableMessage(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 503 || typeof error.detail !== 'object' || error.detail === null) return null
  const detail = error.detail as Record<string, unknown>
  const code = typeof detail.code === 'string' ? detail.code : 'snapshot_unavailable'
  const reason = typeof detail.reason === 'string' ? detail.reason : error.message
  return `${code}: ${reason}`
}

/** One saved, Ledger-bound read model for the complete feeder cockpit. */
export function getDbrFeederCockpit() {
  return api<DbrFeederCockpit>('/v1/dbr/feeder/cockpit')
}

/** Immutable purchase obligations for one accepted Item Ledger generation. */
export function getDbrPurchaseCockpit() {
  return api<DbrPurchaseCockpit>('/v1/dbr/purchase/cockpit')
}

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

export function getDbrBoard() {
  return api<DbrBoard>('/v1/dbr/drum/active/board')
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

export function releaseDbrSlot(slotId: number, dryRun: boolean) {
  return api<DbrReleaseResult>(
    `/v1/dbr/drum/slots/${slotId}/release?dry_run=${dryRun ? 'true' : 'false'}`,
    { method: 'POST' },
  )
}

export function releaseDbrDay(scheduleId: number, day: string, dryRun: boolean) {
  return api<DbrReleaseDayResult>(`/v1/dbr/drum/${scheduleId}/release-day`, {
    method: 'POST',
    body: JSON.stringify({ day, dry_run: dryRun }),
  })
}

// ── Feeder-chain positions (read/preview + explicit safe rebuild) ─────────────

export function listDbrFeederPositions(filters: DbrFeederFilters = {}) {
  const search = new URLSearchParams({ include_live_nfp: 'true' })
  if (filters.active_only !== undefined) search.set('active_only', String(filters.active_only))
  if (filters.mode) search.set('mode', filters.mode)
  if (filters.supply) search.set('supply', filters.supply)
  if (filters.zone) search.set('zone', filters.zone)
  if (filters.search?.trim()) search.set('search', filters.search.trim())
  if (filters.limit !== undefined) search.set('limit', String(filters.limit))
  if (filters.offset !== undefined) search.set('offset', String(filters.offset))
  return api<DbrFeederPosition[]>(`/v1/dbr/feeder/positions?${search.toString()}`)
}

export function getDbrProcessingBoard() {
  return api<DbrProcessingBoard>('/v1/dbr/feeder/processing/board')
}

export function previewDbrProcessingChain() {
  return api<DbrProcessingChainPreview>('/v1/dbr/feeder/processing/chain/preview', { method: 'POST' })
}

export function previewDbrProcessingOrder(signalId: number) {
  return api<DbrProcessingOrderPreview>(`/v1/dbr/feeder/signals/${signalId}/processing-order-preview`)
}

export function getDbrProcessingTripManifest() {
  return api<DbrProcessingTripManifest>('/v1/dbr/feeder/processing/trip-manifest')
}

export function getDbrProcessingTripManifestPrint() {
  return apiText('/v1/dbr/feeder/processing/trip-manifest/print')
}

export function previewDbrFeederPositions(scheduleId?: number) {
  return api<DbrFeederPreview>('/v1/dbr/feeder/positions/preview', {
    method: 'POST',
    body: JSON.stringify({ schedule_id: scheduleId ?? null }),
  })
}

export function rebuildDbrFeederPositions(expectedScheduleId: number) {
  return api<DbrFeederPreview>('/v1/dbr/feeder/positions/rebuild', {
    method: 'POST',
    body: JSON.stringify({ schedule_id: expectedScheduleId, expected_schedule_id: expectedScheduleId }),
  })
}

// ── Feeder-chain advisory signals (preview + explicit refresh, read-only use) ─

export function previewDbrFeederSignals() {
  return api<DbrFeederSignalPreview>('/v1/dbr/feeder/signals/preview', {
    method: 'POST',
  })
}

export function refreshDbrFeederSignals(expectedScheduleId: number) {
  return api<DbrFeederSignalPreview>('/v1/dbr/feeder/signals/refresh', {
    method: 'POST',
    body: JSON.stringify({ expected_schedule_id: expectedScheduleId }),
  })
}

export function listDbrFeederSignals(filters: DbrFeederSignalFilters = {}) {
  const search = new URLSearchParams()
  if (filters.status) search.set('status', filters.status)
  if (filters.zone) search.set('zone', filters.zone)
  if (filters.signal_type) search.set('signal_type', filters.signal_type)
  if (filters.search?.trim()) search.set('search', filters.search.trim())
  if (filters.limit !== undefined) search.set('limit', String(filters.limit))
  if (filters.offset !== undefined) search.set('offset', String(filters.offset))
  const query = search.toString()
  return api<DbrFeederSignal[]>(`/v1/dbr/feeder/signals${query ? `?${query}` : ''}`)
}

export function getDbrFeederSignal(signalId: number) {
  return api<DbrFeederSignal>(`/v1/dbr/feeder/signals/${signalId}`)
}

// ── Feeder material readiness + chain explosion (advisory, no 1С writes) ──────

export function getDbrFeederDeficits() {
  return api<DbrFeederDeficitsResult>('/v1/dbr/feeder/deficits')
}

export function previewDbrFeederChain() {
  return api<DbrChainPreview>('/v1/dbr/feeder/chain/preview', {
    method: 'POST',
  })
}

export function refreshDbrFeederChain() {
  return api<DbrChainRefresh>('/v1/dbr/feeder/chain/refresh', {
    method: 'POST',
  })
}

// ── Materialization into 1С (Фаза 3): launch production / purchase orders ──────

export function launchDbrSignal(signalId: number, dryRun: boolean) {
  return api<DbrSignalLaunchResult>(`/v1/dbr/feeder/signals/${signalId}/launch`, {
    method: 'POST',
    body: JSON.stringify({ dry_run: dryRun }),
  })
}

export function launchDbrPurchase(signalIds: number[] | undefined, dryRun: boolean) {
  return api<DbrPurchaseLaunchResult>('/v1/dbr/feeder/purchase/launch', {
    method: 'POST',
    body: JSON.stringify({ signal_ids: signalIds ?? null, dry_run: dryRun }),
  })
}

// ── Net purchase plan (preview + materialize supplier orders) ──────────────────

export function previewDbrPurchasePlan(params: {
  programId?: number
  active?: boolean
  thresholdDays: number
}) {
  const search = new URLSearchParams()
  if (params.programId != null) search.set('program_id', String(params.programId))
  if (params.active) search.set('active', 'true')
  search.set('threshold_days', String(params.thresholdDays))
  return api<DbrPurchasePlanPreview>(`/v1/dbr/purchase-plan/preview?${search.toString()}`)
}

export function materializeDbrPurchasePlan(params: {
  programId?: number
  active?: boolean
  thresholdDays: number
  dryRun: boolean
}) {
  return api<DbrPurchaseLaunchResult>('/v1/dbr/purchase-plan/materialize', {
    method: 'POST',
    body: JSON.stringify({
      program_id: params.programId ?? null,
      active: params.active ?? false,
      threshold_days: params.thresholdDays,
      dry_run: params.dryRun,
    }),
  })
}
