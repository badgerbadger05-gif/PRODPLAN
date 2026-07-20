import type { DetailLayout } from './types'
import { formatField } from './fieldFormat'

type Props<T> = {
  value: T
  layout: DetailLayout<T>
}

export function FormRenderer<T>({ value, layout }: Props<T>) {
  return (
    <>
      {layout.sections.map((section, sectionIndex) => (
        <section key={section.title ?? sectionIndex}>
          {section.title && <h3>{section.title}</h3>}
          {section.fields && (
            <div className="detailGrid">
              {section.fields.map((field) => (
                <div className={field.span === 2 ? 'doctypeField doctypeFieldWide' : 'doctypeField'} key={String(field.key)}>
                  <span>{field.label}</span>
                  <strong>{formatField(value[field.key], field.type, field.options)}</strong>
                </div>
              ))}
            </div>
          )}
        </section>
      ))}
    </>
  )
}

