import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
}

export function FilterBar<Row, Filters extends object, Detail>({ doctype, state }: Props<Row, Filters, Detail>) {
  if (!doctype.filters?.length) return null

  return (
    <div className="columnFilterSearch doctypeFilterBar">
      {doctype.filters.map((filter, index) => {
        if (filter.kind === 'search') {
          return (
            <label className="columnFilterControl" key={`${String(filter.field)}-${index}`}>
              <span>Поиск</span>
              <input
                value={String(state.filters[filter.field] ?? '')}
                placeholder={filter.placeholder}
                onChange={(event) => state.setFilter(filter.field, event.target.value as never)}
              />
            </label>
          )
        }
        if (filter.kind === 'select') {
          return (
            <label className="columnFilterControl" key={`${String(filter.field)}-${index}`}>
              <span>{filter.label}</span>
              <select
                value={String(state.filters[filter.field] ?? '')}
                onChange={(event) => state.setFilter(filter.field, event.target.value as never)}
              >
                {filter.allowEmpty && <option value="">Все</option>}
                {filter.options.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
          )
        }
        if (filter.kind === 'toggle') {
          return (
            <label className="columnFilterControl" key={`${String(filter.field)}-${index}`}>
              <span>{filter.label}</span>
              <input
                type="checkbox"
                checked={Boolean(state.filters[filter.field])}
                onChange={(event) => state.setFilter(filter.field, event.target.checked as never)}
              />
            </label>
          )
        }
        return (
          <div className="columnFilterControl" key={`${String(filter.fieldFrom)}-${index}`}>
            <span>{filter.label}</span>
            <div>
              <input
                type="date"
                value={String(state.filters[filter.fieldFrom] ?? '')}
                onChange={(event) => state.setFilter(filter.fieldFrom, event.target.value as never)}
              />
              <input
                type="date"
                value={String(state.filters[filter.fieldTo] ?? '')}
                onChange={(event) => state.setFilter(filter.fieldTo, event.target.value as never)}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

