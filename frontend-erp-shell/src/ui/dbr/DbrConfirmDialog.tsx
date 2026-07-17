import type { ReactNode } from 'react'

// Two-step "preview → confirm" modal shared by every DBR flow that writes to 1С.
//
// phase='preview' shows the document preview plus an explicit red confirm button
// and a live-1С warning; phase='done' shows the write result and only a Close
// button, so a confirmed write can never be mistaken for a preview; phase='blocked'
// shows a refusal (e.g. a material deficit) with only a Close button and no
// confirm. The confirm button is disabled while a request is in flight (`busy`).
export function DbrConfirmDialog({
  title,
  phase,
  busy,
  confirmLabel = 'Провести в 1С',
  error,
  children,
  onClose,
  onConfirm,
}: {
  title: string
  phase: 'preview' | 'done' | 'blocked'
  busy: boolean
  confirmLabel?: string
  error?: string
  children: ReactNode
  onClose: () => void
  onConfirm: () => void
}) {
  return (
    <div
      className="dialogOverlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={busy ? undefined : onClose}
    >
      <div className="dialogBox dbrConfirmBox" onClick={(e) => e.stopPropagation()}>
        <div className={`dialogHeader${phase === 'done' ? ' dbrDoneHeader' : ''}`}>{title}</div>
        <div className="dialogBody">
          {phase === 'preview' && (
            <div className="dbrLiveWarn">
              ⚠ Будет создан документ в живой 1С. Проверьте данные перед подтверждением.
            </div>
          )}
          {phase === 'done' && (
            <div className="dbrDoneBanner">✓ Документ проведён в живой 1С. Это уже не предпросмотр.</div>
          )}
          {error && <div className="dialogError">{error}</div>}
          {children}
        </div>
        <div className="dialogFooter">
          <button onClick={onClose} disabled={busy}>
            {phase === 'preview' ? 'Отмена' : 'Закрыть'}
          </button>
          {phase === 'preview' && (
            <button className="dbrDanger" onClick={onConfirm} disabled={busy}>
              {busy ? 'Отправка…' : confirmLabel}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
