import type { ReactNode } from 'react'

type Props = {
  loading: boolean
  error?: string
  empty: boolean
  loadingLabel: string
  emptyLabel: string
  onRetry?: () => void
  announce?: boolean
  children: ReactNode
}

export function AsyncState({
  loading,
  error,
  empty,
  loadingLabel,
  emptyLabel,
  onRetry,
  announce = true,
  children,
}: Props) {
  if (loading && empty) {
    return (
      <div
        className="asyncState asyncStateLoading"
        role={announce ? 'status' : undefined}
        aria-live={announce ? 'polite' : undefined}
      >
        <span className="asyncStateSpinner" aria-hidden="true" />
        <strong>{loadingLabel}</strong>
        <span>Ответ может занять несколько секунд. Данные ещё не получены.</span>
      </div>
    )
  }

  if (error && empty) {
    return (
      <div className="asyncState asyncStateError" role="alert">
        <strong>Не удалось загрузить данные</strong>
        <span>{error}</span>
        {onRetry && <button type="button" onClick={onRetry}>Повторить</button>}
      </div>
    )
  }

  if (empty) {
    return (
      <div className="asyncState asyncStateEmpty" role={announce ? 'status' : undefined}>
        <strong>{emptyLabel}</strong>
        <span>Запрос завершён: подходящих строк нет.</span>
      </div>
    )
  }

  return <div className="asyncStateContent" aria-busy={loading}>{children}</div>
}
