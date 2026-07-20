import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { CommandBar } from './CommandBar'
import { DoctypeTable } from './DoctypeTable'
import { FilterBar } from './FilterBar'
import { FormRenderer } from './FormRenderer'
import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import type { AccessSubject } from './permissions'
import { canView } from './permissions'
import type { ReactNode } from 'react'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  breadcrumbs?: string
  access: AccessSubject
  renderDetail?: (value: Detail | Row, state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderDialog?: (dialog: NonNullable<DoctypeListState<Row, Filters, Detail>['dialog']>, close: () => void) => ReactNode
  onRowDoubleClick?: (row: Row) => void
}

export function DoctypePage<Row, Filters extends object, Detail>({
  doctype,
  state,
  breadcrumbs,
  access,
  renderDetail,
  renderDialog,
  onRowDoubleClick,
}: Props<Row, Filters, Detail>) {
  const detailValue = state.detail ?? state.activeRow
  if (!canView(doctype.permissions, access)) {
    return (
      <main className="workArea">
        <div className="errorLine" role="alert">Нет доступа к разделу «{doctype.meta.title}»</div>
      </main>
    )
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">{breadcrumbs ?? doctype.meta.title}</div>
        <div className="runBadge">строк: {state.paging.total}</div>
      </div>
      <DocumentWindow
        title={doctype.meta.title}
        subtitle={doctype.meta.subtitle}
        hotkeys={doctype.meta.hotkeys}
        footer={(
          <StatusBar
            loading={state.loading}
            visibleFrom={state.paging.visibleFrom}
            visibleTo={state.paging.visibleTo}
            total={state.paging.total}
            selectedCount={doctype.meta.selectionMode === 'single' ? (state.activeRow ? 1 : 0) : state.selection.length}
            canPrev={state.paging.canPrev}
            canNext={state.paging.canNext}
            onPrev={state.paging.prev}
            onNext={state.paging.next}
          />
        )}
      >
        <CommandBar doctype={doctype} state={state} access={access} />
        <FilterBar doctype={doctype} state={state} />
        {state.error && <div className="errorLine">{state.error}</div>}
        {state.message && <div className="successLine">{state.message}</div>}
        <div className={doctype.detail ? 'split' : undefined}>
          <DoctypeTable doctype={doctype} state={state} onRowDoubleClick={onRowDoubleClick} />
          {doctype.detail && (
            <aside className="detailPane">
              {state.detailLoading && <div>Загрузка...</div>}
              {!state.detailLoading && detailValue && (
                renderDetail
                  ? renderDetail(detailValue, state)
                  : <FormRenderer value={detailValue} layout={doctype.detail} />
              )}
            </aside>
          )}
        </div>
        {state.dialog && renderDialog?.(state.dialog, state.closeDialog)}
      </DocumentWindow>
    </main>
  )
}
