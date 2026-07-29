// Presentation models only. Backend DTOs are generated from OpenAPI and must be
// adapted into these models at the services boundary once ledger endpoints land.

export type LedgerPostingView = {
  id: string
  occurredAt: string
  itemLabel: string
  poolKey: string
  eventType: string
  quantityDelta: number
  unit?: string | null
  sourceDocument: string
  sourceLine?: string | null
  correlationId?: string | null
  reversalOf?: string | null
}

export type LedgerBalanceView = {
  poolKey: string
  poolLabel: string
  quantity: number
  unit?: string | null
  calculatedAt: string
}

export type ProvenanceStepView = {
  id: string
  occurredAt: string
  kind: 'source' | 'command' | 'posting' | 'projection' | 'reversal'
  title: string
  detail?: string | null
  actor?: string | null
  correlationId?: string | null
}

export type ReconciliationIssueView = {
  id: string
  severity: 'info' | 'warning' | 'error'
  poolKey: string
  itemLabel: string
  ledgerQuantity: number
  projectionQuantity: number
  difference: number
  status: 'open' | 'acknowledged' | 'resolved'
  detectedAt: string
}

export type AuditEventView = {
  id: string
  occurredAt: string
  actor: string
  action: string
  source: string
  correlationId?: string | null
  changes: Array<{
    field: string
    before?: string | null
    after?: string | null
  }>
}
