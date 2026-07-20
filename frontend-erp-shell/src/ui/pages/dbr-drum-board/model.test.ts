import { describe, expect, it } from 'vitest'
import type { DbrBoardSlot, DbrReleaseResult } from '../../../domain/dbr'
import {
  dayLabel,
  drumSlotReleaseState,
  drumSlotShortageTitle,
  groupDrumSlotsByCell,
  indexDrumSlotsById,
  isWeekend,
  releaseResultText,
} from './model'

const slot = (overrides: Partial<DbrBoardSlot> = {}): DbrBoardSlot => ({
  id: 1,
  date: '2026-07-20',
  resource_id: 11,
  item_id: 501,
  item_code: 'PUMP-01',
  item_name: 'Насос',
  qty: 10,
  produced_qty: 2,
  kit_status: 'green',
  release_status: 'pending',
  shortage: [],
  position: 1,
  ...overrides,
})

describe('DBR drum board model', () => {
  it('formats day headings and classifies weekends', () => {
    expect(isWeekend('2026-07-18')).toBe(true)
    expect(isWeekend('2026-07-19')).toBe(true)
    expect(isWeekend('2026-07-20')).toBe(false)
    expect(dayLabel('2026-07-20')).toMatch(/^20\.07 /)
  })

  it('indexes slots by board cell and id without changing their order', () => {
    const slots = [
      slot({ id: 1 }),
      slot({ id: 2 }),
      slot({ id: 3, date: '2026-07-21' }),
    ]

    expect(groupDrumSlotsByCell(slots).get('11::2026-07-20')?.map((row) => row.id)).toEqual([1, 2])
    expect(groupDrumSlotsByCell(slots).get('11::2026-07-21')?.map((row) => row.id)).toEqual([3])
    expect(indexDrumSlotsById(slots).get(2)).toBe(slots[1])
  })

  it('builds the shortage tooltip with quantities and optional warehouse', () => {
    expect(drumSlotShortageTitle(slot({
      shortage: [
        { item: 'Подшипник', required: 4, available: 1, warehouse: 'Основной' },
        { item: 'Втулка', required: 2, available: 0 },
      ],
    }))).toBe('Подшипник: нужно 4, есть 1 (Основной)\nВтулка: нужно 2, есть 0')
    expect(drumSlotShortageTitle(slot())).toBe('')
  })

  it('derives release availability from kit and release statuses', () => {
    expect(drumSlotReleaseState(slot())).toEqual({
      status: 'pending',
      alreadyReleased: false,
      canRelease: true,
    })
    expect(drumSlotReleaseState(slot({ kit_status: 'red' })).canRelease).toBe(false)
    expect(drumSlotReleaseState(slot({ release_status: 'released' }))).toEqual({
      status: 'released',
      alreadyReleased: true,
      canRelease: false,
    })
    expect(drumSlotReleaseState(slot({ release_status: 'completed' })).alreadyReleased).toBe(true)
  })

  it('formats preview, success, duplicate, conflict and error report rows', () => {
    const result = { number: 'ERP-101' } as DbrReleaseResult
    expect(releaseResultText(result, false)).toBe('готов к релизу')
    expect(releaseResultText({ ...result, created: true }, true)).toBe('Заказ № ERP-101')
    expect(releaseResultText({ ...result, already_released: true }, true)).toBe('Уже создан № ERP-101')
    expect(releaseResultText({ ...result, conflict: 'не green' }, true)).toBe('Отказ: не green')
    expect(releaseResultText({ ...result, error: '1С недоступна' }, true)).toBe('Ошибка: 1С недоступна')
    expect(releaseResultText({ ...result, note: 'пропущено' }, true)).toBe('пропущено')
  })
})
