import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
}

export function CommandBar<Row, Filters extends object, Detail>({ doctype, state }: Props<Row, Filters, Detail>) {
  const actions = (doctype.actions ?? []).filter(
    (action) => action.visible?.(state.actionContext) !== false,
  )

  return (
    <div className="commandBar">
      {actions.map((action) => (
        <button
          key={action.key}
          className={action.tone === 'primary' ? 'primary' : undefined}
          disabled={state.loading || action.enabled?.(state.actionContext) === false}
          onClick={() => void state.runAction(action.key)}
        >
          {action.label}
        </button>
      ))}
      <button onClick={state.reload} disabled={state.loading}>Обновить</button>
      {doctype.renderExtraToolbar?.(state.actionContext)}
    </div>
  )
}

