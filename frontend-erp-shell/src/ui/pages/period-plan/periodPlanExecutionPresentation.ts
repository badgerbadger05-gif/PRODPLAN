import type { ExecutionJournalRow, ExecutionWorkItem } from '../../../domain/planning'

export type ExecutionWorkItemPresentation = {
  href: string | null
  assignedQty: number | null
  unassignedQty: number | null
  unavailableReason: string | null
}

export function executionWorkItemPresentation(
  item: Pick<ExecutionWorkItem, 'type' | 'qty' | 'assigned_qty' | 'unassigned_qty' | 'current_identity' | 'navigation_href' | 'navigation_reason'>,
): ExecutionWorkItemPresentation {
  return {
    href: item.navigation_href ?? null,
    assignedQty: item.assigned_qty ?? null,
    unassignedQty: item.unassigned_qty ?? null,
    unavailableReason: item.navigation_reason ?? null,
  }
}

export function executionJournalRowPresentation(
  row: Pick<ExecutionJournalRow, 'status_label' | 'explanations' | 'current_identity' | 'source_revision' | 'unassigned_qty'>,
) {
  return {
    statusLabel: row.status_label ?? null,
    explanations: row.explanations ?? [],
    currentIdentity: row.current_identity ?? null,
    sourceRevision: row.source_revision ?? null,
    unassignedQty: row.unassigned_qty ?? null,
    unavailable: !row.status_label || !row.current_identity || !row.source_revision,
  }
}
