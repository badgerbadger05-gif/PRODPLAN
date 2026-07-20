import type {
  AuditEventView,
  LedgerBalanceView,
  LedgerPostingView,
  ProvenanceStepView,
  ReconciliationIssueView,
} from '../ui/ledger'

export type LedgerPostingFilters = {
  search: string
  eventType: string
  direction: '' | 'receipt' | 'issue'
}

export type LedgerPostingDetailView = {
  posting: LedgerPostingView
  balance: LedgerBalanceView
  provenance: ProvenanceStepView[]
  reversalChain: LedgerPostingView[]
  auditTrail: AuditEventView[]
}

export type LedgerWorkspaceSnapshot = {
  postings: LedgerPostingView[]
  issues: ReconciliationIssueView[]
  calculatedAt: string
}

export interface LedgerDataProvider {
  loadSnapshot(filters: LedgerPostingFilters, signal?: AbortSignal): Promise<LedgerWorkspaceSnapshot>
  loadPosting(id: string, signal?: AbortSignal): Promise<LedgerPostingDetailView>
}

const postings: LedgerPostingView[] = [
  {
    id: 'P-1042',
    occurredAt: '2026-07-20T10:24:00Z',
    itemLabel: 'Подшипник ведущего вала',
    poolKey: 'ITEM-9:MAIN',
    eventType: 'Поступление',
    quantityDelta: 12,
    unit: 'шт',
    sourceDocument: 'Поступление ЗП-000008',
    sourceLine: 'Строка 1',
    correlationId: 'corr-purchase-008',
  },
  {
    id: 'P-1043',
    occurredAt: '2026-07-20T11:05:00Z',
    itemLabel: 'Подшипник ведущего вала',
    poolKey: 'ITEM-9:MAIN',
    eventType: 'Резерв производства',
    quantityDelta: -4,
    unit: 'шт',
    sourceDocument: 'Заказ ЗСМ-000943',
    sourceLine: 'Комплектование',
    correlationId: 'corr-order-943',
  },
  {
    id: 'P-1044',
    occurredAt: '2026-07-20T11:18:00Z',
    itemLabel: 'Подшипник ведущего вала',
    poolKey: 'ITEM-9:MAIN',
    eventType: 'Сторно резерва',
    quantityDelta: 4,
    unit: 'шт',
    sourceDocument: 'Корректировка ЗСМ-000943',
    sourceLine: 'Отмена комплектования',
    correlationId: 'corr-order-943-reversal',
    reversalOf: 'P-1043',
  },
]

const issues: ReconciliationIssueView[] = [
  {
    id: 'R-17',
    severity: 'warning',
    poolKey: 'ITEM-9:MAIN',
    itemLabel: 'Подшипник ведущего вала',
    ledgerQuantity: 12,
    projectionQuantity: 8,
    difference: 4,
    status: 'open',
    detectedAt: '2026-07-20T12:00:00Z',
  },
]

function detailFor(posting: LedgerPostingView): LedgerPostingDetailView {
  const isReversal = posting.id === 'P-1044'
  return {
    posting,
    balance: {
      poolKey: posting.poolKey,
      poolLabel: 'Основной склад · Подшипник ведущего вала',
      quantity: 12,
      unit: 'шт',
      calculatedAt: '2026-07-20T12:00:00Z',
    },
    provenance: [
      {
        id: `${posting.id}-source`,
        occurredAt: posting.occurredAt,
        kind: 'source',
        title: posting.sourceDocument,
        detail: posting.sourceLine,
        actor: 'Синхронизация 1С',
        correlationId: posting.correlationId,
      },
      {
        id: `${posting.id}-command`,
        occurredAt: posting.occurredAt,
        kind: isReversal ? 'reversal' : 'command',
        title: isReversal ? 'Команда сторнирования' : 'Проверка идемпотентности',
        detail: isReversal ? `Исходная проводка ${posting.reversalOf}` : 'Повторная обработка не создаст дубликат',
        actor: 'ledger-worker',
        correlationId: posting.correlationId,
      },
      {
        id: `${posting.id}-posting`,
        occurredAt: posting.occurredAt,
        kind: 'posting',
        title: `Неизменяемая проводка ${posting.id}`,
        detail: `${posting.quantityDelta > 0 ? '+' : ''}${posting.quantityDelta} ${posting.unit}`,
        actor: 'ledger',
        correlationId: posting.correlationId,
      },
      {
        id: `${posting.id}-projection`,
        occurredAt: '2026-07-20T12:00:00Z',
        kind: 'projection',
        title: 'Проекция остатка обновлена',
        detail: 'Производное значение доступно только для чтения',
        actor: 'projection-worker',
        correlationId: posting.correlationId,
      },
    ],
    reversalChain: isReversal
      ? postings.filter((row) => row.id === 'P-1043' || row.id === 'P-1044')
      : postings.filter((row) => row.id === posting.id || row.reversalOf === posting.id),
    auditTrail: [
      {
        id: `${posting.id}-accepted`,
        occurredAt: posting.occurredAt,
        actor: 'ledger-worker',
        action: 'Команда принята',
        source: posting.sourceDocument,
        correlationId: posting.correlationId,
        changes: [
          { field: 'quantity', before: null, after: String(posting.quantityDelta) },
          { field: 'posting_created', before: 'нет', after: 'да' },
        ],
      },
      {
        id: `${posting.id}-projected`,
        occurredAt: '2026-07-20T12:00:00Z',
        actor: 'projection-worker',
        action: 'Проекция пересчитана',
        source: posting.poolKey,
        correlationId: posting.correlationId,
        changes: [{ field: 'balance', before: '8', after: '12' }],
      },
    ],
  }
}

export const mockLedgerDataProvider: LedgerDataProvider = {
  async loadSnapshot(filters, signal) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    const search = filters.search.trim().toLocaleLowerCase('ru')
    const filtered = postings.filter((posting) => {
      if (filters.eventType && posting.eventType !== filters.eventType) return false
      if (filters.direction === 'receipt' && posting.quantityDelta <= 0) return false
      if (filters.direction === 'issue' && posting.quantityDelta >= 0) return false
      return !search || [
        posting.id,
        posting.itemLabel,
        posting.poolKey,
        posting.sourceDocument,
        posting.correlationId,
      ].some((value) => value?.toLocaleLowerCase('ru').includes(search))
    })
    return { postings: filtered, issues, calculatedAt: '2026-07-20T12:00:00Z' }
  },
  async loadPosting(id, signal) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    const posting = postings.find((row) => row.id === id)
    if (!posting) throw new Error(`Проводка ${id} не найдена`)
    return detailFor(posting)
  },
}
