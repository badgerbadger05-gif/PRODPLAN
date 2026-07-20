import type {
  DbrFeederDeficit,
  DbrFeederPosition,
  DbrFeederSignal,
  DbrFeederSignalPreview,
} from '../../../domain/dbr'

export const ZONE_LABEL: Record<string, string> = { green: 'Зелёная', yellow: 'Жёлтая', red: 'Красная' }
export const MODE_LABEL: Record<string, string> = { shelf: 'Полка', under_schedule: 'Под график' }
export const SUPPLY_LABEL: Record<string, string> = { purchase: 'Закупка', manufacture: 'Производство', processing: 'Переработка' }
export const REASON_LABEL: Record<string, string> = {
  open_supply_destination_missing: 'не указан склад открытого прихода',
  stale_schedule: 'позиция рассчитана не по активному графику',
  production_inbound_destination_missing: 'не указан склад производственного прихода',
  production_inbound_eta_missing: 'не указана дата производственного прихода',
  supplier_inbound_destination_missing: 'не указан склад прихода поставщика',
  supplier_inbound_eta_missing: 'не указана дата прихода поставщика',
}
export const SIGNAL_TYPE_LABEL: Record<string, string> = { 'Пополнение': 'Пополнение', 'Под график': 'Под график' }
export const MATERIAL_CLASS: Record<string, string> = { ok: 'green', part: 'yellow', no: 'red', q: 'gray' }
export const MATERIAL_TITLE: Record<string, string> = {
  'Готов': 'Комплект обеспечен запасом предприятия',
  'Частично': 'Обеспечена часть комплекта',
  'Дефицит': 'Не хватает материала на комплект',
  'Расписан выше': 'Материал забран сигналами выше по очереди',
}
export const SOURCE_LABEL: Record<string, string> = { make: 'Производство', buy: 'Закупка' }
export const PROCESSING_STAGE_LABEL: Record<string, string> = {
  ordered: 'Заказан',
  transferred: 'Передан переработчику',
  reported: 'Есть выпуск по отчёту',
}

export type DeficitSortKey = 'blocks_signals' | 'short_qty' | 'nearest_due' | 'item'
export type FeederFilters = { search: string; zone: string; mode: string; supply: string }
export type SignalFilters = { search: string; zone: string; status: string; signal_type: string }

export const EMPTY_FILTERS: FeederFilters = { search: '', zone: '', mode: '', supply: '' }
export const EMPTY_SIGNAL_FILTERS: SignalFilters = { search: '', zone: '', status: 'Open', signal_type: '' }

export function zoneKey(value?: string | null) {
  return String(value ?? 'unknown').trim().toLowerCase()
}

export function visibleFeederSignals(signals: DbrFeederSignal[], deficitFilter: string) {
  if (!deficitFilter) return signals
  return signals.filter((signal) => (
    signal.deficit_lines ?? []
  ).some((line) => line.item === deficitFilter))
}

export function purchaseSignalSelection(signals: DbrFeederSignal[], selected: ReadonlySet<number>) {
  const selectableIds = signals
    .filter((signal) => signal.signal_type === 'Пополнение' && signal.status === 'Open')
    .map((signal) => signal.id)
  const selectedIds = selectableIds.filter((id) => selected.has(id))
  return {
    selectableIds,
    selectedIds,
    allSelected: selectableIds.length > 0 && selectedIds.length === selectableIds.length,
  }
}

export function sortFeederDeficits(deficits: DbrFeederDeficit[], sort: DeficitSortKey) {
  const rows = [...deficits]
  rows.sort((a, b) => {
    switch (sort) {
      case 'short_qty': return b.short_qty - a.short_qty
      case 'nearest_due': return (a.nearest_due || '9999') < (b.nearest_due || '9999') ? -1 : 1
      case 'item': return a.item.localeCompare(b.item)
      case 'blocks_signals':
      default: return b.blocks_signals - a.blocks_signals
    }
  })
  return rows
}

export function summarizeFeederPositions(rows: DbrFeederPosition[]) {
  return rows.reduce((summary, row) => {
    const zone = zoneKey(row.live_nfp?.zone)
    summary[zone] = (summary[zone] ?? 0) + 1
    if (!row.live_nfp?.is_complete) summary.incomplete = (summary.incomplete ?? 0) + 1
    return summary
  }, {} as Record<string, number>)
}

export function summarizeSignalPreview(preview: DbrFeederSignalPreview | null) {
  const actionable = preview?.rows.filter((row) => row.action === 'open' || row.action === 'update') ?? []
  return {
    replenish: actionable.filter((row) => row.signal_type === 'Пополнение').length,
    underSchedule: actionable.filter((row) => row.signal_type === 'Под график').length,
  }
}
