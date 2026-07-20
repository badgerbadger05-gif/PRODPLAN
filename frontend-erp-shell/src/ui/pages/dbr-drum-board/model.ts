import type { DbrBoardSlot, DbrReleaseDaySlotResult } from '../../../domain/dbr'
import { qty } from '../../../lib/format'

export const KIT_CLASS: Record<string, string> = {
  green: 'kitGreen',
  yellow: 'kitYellow',
  red: 'kitRed',
  unknown: 'kitUnknown',
}

export function isWeekend(iso: string) {
  const day = new Date(`${iso}T00:00:00`).getDay()
  return day === 0 || day === 6
}

export function dayLabel(iso: string) {
  const date = new Date(`${iso}T00:00:00`)
  const weekday = date.toLocaleDateString('ru-RU', { weekday: 'short' })
  const parts = iso.split('-')
  return `${parts[2]}.${parts[1]} ${weekday}`
}

export function groupDrumSlotsByCell(slots: DbrBoardSlot[]) {
  const map = new Map<string, DbrBoardSlot[]>()
  for (const slot of slots) {
    const key = `${slot.resource_id}::${slot.date}`
    const bucket = map.get(key)
    if (bucket) bucket.push(slot)
    else map.set(key, [slot])
  }
  return map
}

export function indexDrumSlotsById(slots: DbrBoardSlot[]) {
  return new Map(slots.map((slot) => [slot.id, slot]))
}

export function drumSlotShortageTitle(slot: DbrBoardSlot) {
  return (slot.shortage ?? [])
    .map((line) => (
      `${line.item}: нужно ${qty(line.required)}, есть ${qty(line.available)}` +
      (line.warehouse ? ` (${line.warehouse})` : '')
    ))
    .join('\n')
}

export function drumSlotReleaseState(slot: DbrBoardSlot) {
  const status = slot.release_status || 'pending'
  const alreadyReleased = status === 'released' || status === 'completed'
  return {
    status,
    alreadyReleased,
    canRelease: slot.kit_status === 'green' && status === 'pending',
  }
}

export function releaseResultText(result: DbrReleaseDaySlotResult, done: boolean) {
  if (result.conflict) return `Отказ: ${result.conflict}`
  if (result.error) return `Ошибка: ${result.error}`
  if (!done) return 'готов к релизу'
  if (result.created) return `Заказ № ${result.number}`
  if (result.already_released) return `Уже создан № ${result.number}`
  return result.note ?? 'готово'
}
