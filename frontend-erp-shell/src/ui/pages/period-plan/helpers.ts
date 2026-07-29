import { dateRu } from '../../../lib/format'

export type SortDir = 'asc' | 'desc'

export function nextFriday(offset = 0) {
  const d = new Date()
  const dow = d.getDay()
  d.setDate(d.getDate() + ((5 - dow + 7) % 7) + offset * 7)
  return d.toISOString().slice(0, 10)
}

export function bucketLabel(iso: string) {
  return dateRu(iso).slice(0, 5)
}
