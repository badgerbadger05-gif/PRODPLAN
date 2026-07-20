import type { MaterialIssueCreateResponse } from '../../../domain/productionControl'

export const limit = 100
export const coverageDrivenStatuses = new Set(['shortage', 'partial', 'ready'])

export function recordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object') : []
}

export function firstExportProblem(...summaries: Array<Record<string, unknown> | null | undefined>) {
  for (const summary of summaries) {
    if (!summary) continue
    for (const entry of recordArray(summary.entries)) {
      const problem = entry.error || entry.reason
      if (problem) return String(problem)
    }
    for (const row of recordArray(summary.skipped_rows)) {
      const problem = row.error || row.reason
      if (problem) return String(problem)
    }
  }
  return ''
}

export function issueIdsFromCreateResult(result: MaterialIssueCreateResponse) {
  return [
    ...(result.created ?? []).map((row) => row.issue_id),
    ...(result.reused ?? []).map((row) => row.issue_id),
  ].filter(Boolean)
}
