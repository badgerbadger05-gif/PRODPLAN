import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import { canRunAction, type AccessSubject } from './permissions'
import { Button } from '../kit'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  access: AccessSubject
  onExportCsv?: () => void
}

export function CommandBar<Row, Filters extends object, Detail>({ doctype, state, access, onExportCsv }: Props<Row, Filters, Detail>) {
  const actions = (doctype.actions ?? []).filter(
    (action) => action.visible?.(state.actionContext) !== false
      && canRunAction(doctype.permissions, action.key, access),
  )

  return (
    <div className="commandBar">
      {actions.map((action) => (
        <Button
          key={action.key}
          variant={action.tone === 'primary' ? 'primary' : 'default'}
          title={action.disabledReason?.(state.actionContext)}
          disabled={
            state.loading
            || action.enabled?.(state.actionContext) === false
            || (action.scope === 'selection' && state.selection.length === 0)
            || (action.scope === 'row' && !state.activeRow)
          }
          onClick={() => void state.runAction(action.key)}
        >
          {typeof action.label === 'function' ? action.label(state.actionContext) : action.label}
        </Button>
      ))}
      {doctype.meta.exportCsv && (
        <Button onClick={onExportCsv} disabled={state.loading || !state.rows.length}>
          CSV (текущая страница)
        </Button>
      )}
      <Button onClick={state.reload} disabled={state.loading}>Обновить</Button>
      {doctype.renderExtraToolbar?.(state.actionContext)}
    </div>
  )
}
