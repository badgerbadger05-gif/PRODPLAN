import type { DetailLayout } from './types'
import { columnValue, formatField } from './fieldFormat'

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
          {section.table && (
            <table className="journalTable doctypeDetailTable">
              <thead>
                <tr>
                  {section.table.columns.map((column) => (
                    <th key={column.key}>{column.title}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {section.table.rows(value).map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {section.table?.columns.map((column) => (
                      <td key={column.key}>
                        {column.render
                          ? column.render(row)
                          : formatField(columnValue(column, row), column.type, column.options)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </>
  )
}
