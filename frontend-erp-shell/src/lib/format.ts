export function qty(value: unknown) {
  if (value == null || value === '') return '—'
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  return numeric.toLocaleString('ru-RU', { maximumFractionDigits: 3 })
}

export function dateRu(value?: string | null) {
  if (!value) return ''
  const raw = String(value).slice(0, 10)
  const parts = raw.split('-')
  return parts.length === 3 ? `${parts[2]}.${parts[1]}.${parts[0]}` : raw
}

export function dateTimeRu(value?: string | null) {
  if (!value) return ''
  const date = dateRu(value)
  const time = String(value).slice(11, 16)
  return time ? `${date} ${time}` : date
}

export function isoToday() {
  return localIsoDate(new Date())
}

export function shiftIsoDate(value: string, days: number) {
  const date = new Date(`${value}T00:00:00`)
  date.setDate(date.getDate() + days)
  return localIsoDate(date)
}

export function localIsoDate(date: Date) {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}
