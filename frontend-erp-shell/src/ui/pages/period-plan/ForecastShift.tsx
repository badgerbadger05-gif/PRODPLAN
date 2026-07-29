import { dateRu } from '../../../lib/format'

type ForecastInfo = {
  forecast_date?: string | null
  forecast_shift_days?: number | null
  forecast_reason?: string | null
}

export function ForecastShift({ forecast }: { forecast?: ForecastInfo | null }) {
  if (!forecast || forecast.forecast_shift_days === null || forecast.forecast_shift_days === undefined) return null
  const days = Number(forecast.forecast_shift_days)
  if (!Number.isFinite(days) || days === 0) return null
  const cls = days > 5 ? 'late' : days > 0 ? 'warn' : 'early'
  const label = `${days > 0 ? '+' : ''}${days} дн`
  const dateText = forecast.forecast_date ? dateRu(forecast.forecast_date).slice(0, 5) : ''
  const title = [forecast.forecast_reason, forecast.forecast_date ? `прогноз ${dateRu(forecast.forecast_date)}` : null].filter(Boolean).join(' · ')
  return <span className={`forecastShift ${cls}`} title={title}>{label}{dateText ? ` · ${dateText}` : ''}</span>
}
