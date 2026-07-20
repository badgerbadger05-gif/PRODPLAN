import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import { canRunAction, type AccessSubject } from './permissions'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  access: AccessSubject
}

export function CommandBar<Row, Filters extends object, Detail>({ doctype, state, access }: Props<Row, Filters, Detail>) {
  const actions = (doctype.actions ?? []).filter(
    (action) => action.visible?.(state.actionContext) !== false
      && canRunAction(doctype.permissions, action.key, access),
  )

  return (
    <div className="commandBar">
      {actions.map((action) => (
        <button
          key={action.key}
          className={action.tone === 'primary' ? 'primary' : undefined}
          disabled={
            state.loading
            || action.enabled?.(state.actionContext) === false
            || (action.scope === 'selection' && state.selection.length === 0)
            || (action.scope === 'row' && !state.activeRow)
          }
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
