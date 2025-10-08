// Composable: форматирование чисел, количеств и меток статуса (ru-RU)

export type NumberFormatOptions = {
  fractionDigits?: number
}

// Кэш форматтеров Intl по числу знаков после запятой
const numberFormatters = new Map<number, Intl.NumberFormat>()

function getNumberFormatter(fractionDigits: number): Intl.NumberFormat {
  let nf = numberFormatters.get(fractionDigits)
  if (!nf) {
    nf = new Intl.NumberFormat('ru-RU', {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
      useGrouping: true
    })
    numberFormatters.set(fractionDigits, nf)
  }
  return nf
}

/** Безопасное форматирование числа с фиксированным количеством знаков (по умолчанию 3) */
export function formatNumber(value: number | string | null | undefined, options: NumberFormatOptions = {}): string {
  const fd = options.fractionDigits ?? 3
  const n = Number(value ?? 0)
  if (Number.isNaN(n)) return getNumberFormatter(fd).format(0)
  return getNumberFormatter(fd).format(n)
}

/** Формат количества с единицей измерения */
export function formatQty(qty: number | string | null | undefined, unit?: string | null, fractionDigits = 3): string {
  const q = formatNumber(qty, { fractionDigits })
  const u = String(unit ?? '').trim()
  return u ? `${q} ${u}` : q
}

/** Цветовая метка статуса для Quasar */
export function statusColor(status?: string): string {
  const s = String(status ?? '').toUpperCase()
  if (s === 'SUCCESS') return 'positive'
  if (s === 'RUNNING') return 'primary'
  if (s === 'FAILED') return 'negative'
  return 'grey'
}

/** Человекочитаемый текст предупреждения */
export function warnText(w: unknown): string {
  try {
    const anyw: any = w as any
    const code = anyw?.code ? String(anyw.code) : ''
    const msg = anyw?.msg ? String(anyw.msg) : ''
    return code ? `${code}: ${msg}` : (msg || String(w))
  } catch {
    return String(w)
  }
}

/** Вернуть безопасную ISO‑дату YYYY-MM-DD или null */
export function safeIsoDate(dateStr?: string | null): string | null {
  if (!dateStr) return null
  return String(dateStr).slice(0, 10) || null
}

/** Комбинированный composable */
export function useFormatting() {
  return { formatNumber, formatQty, statusColor, warnText, safeIsoDate }
}