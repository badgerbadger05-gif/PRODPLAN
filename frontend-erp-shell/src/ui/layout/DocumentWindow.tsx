import type { ReactNode } from 'react'

type Props = {
  title: string
  subtitle: string
  hotkeys?: string
  children: ReactNode
  footer: ReactNode
}

export function DocumentWindow({ title, subtitle, hotkeys, children, footer }: Props) {
  return (
    <section className="documentWindow">
      <header className="docHeader">
        <div>
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
        {hotkeys && <div className="docHotkeys">{hotkeys}</div>}
      </header>
      {children}
      {footer}
    </section>
  )
}
