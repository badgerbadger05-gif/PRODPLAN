import { describe, expect, it } from 'vitest'
import type {
  DbrFeederDeficit,
  DbrFeederPosition,
  DbrFeederSignal,
  DbrFeederSignalPreview,
} from '../../../domain/dbr'
import {
  purchaseSignalSelection,
  sortFeederDeficits,
  summarizeFeederPositions,
  summarizeSignalPreview,
  visibleFeederSignals,
  zoneKey,
} from './model'

const signal = (id: number, signalType: string, status: string, deficitItem?: string): DbrFeederSignal => ({
  id,
  dedup_key: `signal-${id}`,
  signal_type: signalType,
  position_id: id,
  item_id: id,
  warehouse_ref1c: 'MAIN',
  status,
  suggested_qty: 1,
  priority: 1,
  zone: 'red',
  kit_force: false,
  kit_shortage_qty: 0,
  can_launch: false,
  deficit_lines: deficitItem ? [{
    item: deficitItem,
    item_name: deficitItem,
    article: deficitItem,
    need: 2,
    have: 1,
    gross: 1,
    kind: 'buy',
    level: '1',
    cls: 'no',
    buffered: false,
  }] : [],
})

describe('DBR feeder model', () => {
  it('normalizes zones and summarizes incomplete positions', () => {
    const positions = [
      { live_nfp: { zone: ' Green ', is_complete: true } },
      { live_nfp: { zone: 'RED', is_complete: false } },
      { live_nfp: null },
    ] as DbrFeederPosition[]

    expect(zoneKey(' Yellow ')).toBe('yellow')
    expect(zoneKey(null)).toBe('unknown')
    expect(summarizeFeederPositions(positions)).toEqual({
      green: 1,
      red: 1,
      unknown: 1,
      incomplete: 2,
    })
  })

  it('filters deficit drill-down signals and derives purchase selection', () => {
    const signals = [
      signal(1, 'Пополнение', 'Open', 'BEARING'),
      signal(2, 'Пополнение', 'Cancelled', 'BEARING'),
      signal(3, 'Под график', 'Open', 'SHAFT'),
    ]

    const visible = visibleFeederSignals(signals, 'BEARING')
    expect(visible.map((row) => row.id)).toEqual([1, 2])
    expect(visibleFeederSignals(signals, '')).toBe(signals)
    expect(purchaseSignalSelection(visible, new Set([1, 3]))).toEqual({
      selectableIds: [1],
      selectedIds: [1],
      allSelected: true,
    })
    expect(purchaseSignalSelection([signals[1]], new Set())).toEqual({
      selectableIds: [],
      selectedIds: [],
      allSelected: false,
    })
  })

  it('sorts a copy of deficits by every supported operational key', () => {
    const deficits = [
      { item: 'B', short_qty: 2, blocks_signals: 1, nearest_due: '2026-07-25' },
      { item: 'A', short_qty: 5, blocks_signals: 3, nearest_due: '2026-07-22' },
      { item: 'C', short_qty: 1, blocks_signals: 2, nearest_due: null },
    ] as DbrFeederDeficit[]

    expect(sortFeederDeficits(deficits, 'blocks_signals').map((row) => row.item)).toEqual(['A', 'C', 'B'])
    expect(sortFeederDeficits(deficits, 'short_qty').map((row) => row.item)).toEqual(['A', 'B', 'C'])
    expect(sortFeederDeficits(deficits, 'nearest_due').map((row) => row.item)).toEqual(['A', 'B', 'C'])
    expect(sortFeederDeficits(deficits, 'item').map((row) => row.item)).toEqual(['A', 'B', 'C'])
    expect(deficits.map((row) => row.item)).toEqual(['B', 'A', 'C'])
  })

  it('summarizes only open and update preview actions', () => {
    const preview = {
      rows: [
        { action: 'open', signal_type: 'Пополнение' },
        { action: 'update', signal_type: 'Под график' },
        { action: 'cancel', signal_type: 'Пополнение' },
      ],
    } as DbrFeederSignalPreview

    expect(summarizeSignalPreview(preview)).toEqual({ replenish: 1, underSchedule: 1 })
    expect(summarizeSignalPreview(null)).toEqual({ replenish: 0, underSchedule: 0 })
  })
})
