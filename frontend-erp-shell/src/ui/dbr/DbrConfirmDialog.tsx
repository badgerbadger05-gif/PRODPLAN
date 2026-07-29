import { type ReactNode, useEffect, useRef } from 'react'

const focusableSelector = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

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
  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  const busyRef = useRef(busy)

  useEffect(() => {
    onCloseRef.current = onClose
    busyRef.current = busy
  }, [busy, onClose])

  useEffect(() => {
    const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current
    const firstFocusable = panel?.querySelector<HTMLElement>(focusableSelector)
    ;(firstFocusable ?? panel)?.focus()

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        if (busyRef.current) return
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab' || !panel) return
      const focusable = [...panel.querySelectorAll<HTMLElement>(focusableSelector)]
      if (!focusable.length) {
        event.preventDefault()
        panel.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !panel.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      returnFocus?.focus()
    }
  }, [])

  useEffect(() => {
    if (busy || document.activeElement !== panelRef.current) return
    panelRef.current?.querySelector<HTMLElement>(focusableSelector)?.focus()
  }, [busy])

  return (
    <div
      className="dialogOverlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={busy ? undefined : onClose}
    >
      <div
        ref={panelRef}
        className="dialogBox dbrConfirmBox"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
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
