import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { CommandBar } from './CommandBar'
import { DoctypeTable } from './DoctypeTable'
import { FilterBar } from './FilterBar'
import { FormRenderer } from './FormRenderer'
import { SavedViewsBar } from './SavedViewsBar'
import { DialogHost, type DialogRegistry } from '../platform'
import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import type { AccessSubject } from './permissions'
import { canView } from './permissions'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { decodeViewState, encodeViewState, type ViewFilterValue, type ViewState } from '../views'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  breadcrumbs?: string
  access: AccessSubject
  renderDetail?: (value: Detail | Row, state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderDialog?: (dialog: NonNullable<DoctypeListState<Row, Filters, Detail>['dialog']>, close: () => void) => ReactNode
  dialogRegistry?: DialogRegistry
  onRowDoubleClick?: (row: Row) => void
  renderFilters?: (state: DoctypeListState<Row, Filters, Detail>) => ReactNode
}

export function DoctypePage<Row, Filters extends object, Detail>({
  doctype,
  state,
  breadcrumbs,
  access,
  renderDetail,
  renderDialog,
  dialogRegistry = {},
  onRowDoubleClick,
  renderFilters,
}: Props<Row, Filters, Detail>) {
  const [searchParams, setSearchParams] = useSearchParams()
  const columnOptions = useMemo(
    () => doctype.columns.map(({ key, title }) => ({ key, title })),
    [doctype.columns],
  )
  const allColumnKeys = useMemo(() => columnOptions.map(({ key }) => key), [columnOptions])
  const [visibleColumns, setVisibleColumns] = useState<readonly string[]>(allColumnKeys)
  const [density, setDensity] = useState<'compact' | 'comfortable'>('compact')
  const [urlHydrated, setUrlHydrated] = useState(false)
  const [hasUrlView, setHasUrlView] = useState(false)
  const applyViewState = state.applyViewState
  const applyView = useCallback((view: ViewState) => {
    applyViewState(view)
    const available = view.visibleColumns.filter((key) => allColumnKeys.includes(key))
    setVisibleColumns(available.length ? available : allColumnKeys)
    setDensity(view.density)
  }, [allColumnKeys, applyViewState])
  const detailValue = doctype.dataSource.detail ? state.detail : state.activeRow
  const currentView = useMemo<ViewState>(() => ({
    filters: state.filters as ViewState['filters'],
    sort: state.sort ? [{ field: state.sort.sortBy, direction: state.sort.sortDir }] : [],
    visibleColumns,
    density,
  }), [density, state.filters, state.sort, visibleColumns])

  useEffect(() => {
    const token = searchParams.get('view')
    const decoded = token && token.length <= 4096 ? decodeViewState(token) : null
    if (decoded) {
      const filterKeys = new Set(Object.keys(doctype.initialFilters))
      const columnKeys = new Set(allColumnKeys)
      const filters = Object.fromEntries(
        Object.entries(decoded.filters).filter(([key, value]) => (
          filterKeys.has(key)
          && (typeof value !== 'string' || value.length <= 512)
          && (!Array.isArray(value) || value.length <= 100)
        )),
      ) as Record<string, ViewFilterValue>
      const sort = decoded.sort
        .filter((item) => columnKeys.has(item.field))
        .slice(0, 1)
      const columns = [...new Set(decoded.visibleColumns.filter((key) => columnKeys.has(key)))]
      applyView({
        filters: { ...doctype.initialFilters, ...filters } as ViewState['filters'],
        sort,
        visibleColumns: columns.length ? columns : allColumnKeys,
        density: decoded.density,
      })
      setHasUrlView(true)
    }
    setUrlHydrated(true)
    // URL hydration is intentionally one-shot for this mounted resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!urlHydrated) return
    const token = encodeViewState(currentView)
    if (searchParams.get('view') === token) return
    const next = new URLSearchParams(searchParams)
    next.set('view', token)
    setSearchParams(next, { replace: true })
  }, [currentView, searchParams, setSearchParams, urlHydrated])
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
        <SavedViewsBar
          resource={doctype.meta.name}
          initialFilters={doctype.initialFilters}
          filters={state.filters}
          sort={state.sort}
          columns={columnOptions}
          visibleColumns={visibleColumns}
          density={density}
          onApply={applyView}
          onVisibleColumnsChange={setVisibleColumns}
          onDensityChange={setDensity}
          suppressDefaultApply={hasUrlView}
          onCopyLink={() => void navigator.clipboard?.writeText(window.location.href)}
        />
        {renderFilters ? renderFilters(state) : <FilterBar doctype={doctype} state={state} />}
        {state.error && <div className="errorLine">{state.error}</div>}
        {state.message && <div className="successLine">{state.message}</div>}
        <div className={doctype.detail ? 'split' : undefined}>
          <DoctypeTable
            doctype={doctype}
            state={state}
            onRowDoubleClick={onRowDoubleClick}
            visibleColumns={visibleColumns}
            density={density}
          />
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
        {state.dialog && (
          renderDialog
            ? renderDialog(state.dialog, state.closeDialog)
            : (
                <DialogHost
                  dialog={{
                    name: state.dialog.dialog,
                    props: (
                      state.dialog.payload !== null
                      && typeof state.dialog.payload === 'object'
                      && !Array.isArray(state.dialog.payload)
                    )
                      ? state.dialog.payload
                      : { payload: state.dialog.payload },
                    accessibleName: state.dialog.accessibleName ?? state.dialog.dialog,
                  } as never}
                  registry={dialogRegistry}
                  onClose={state.closeDialog}
                />
              )
        )}
      </DocumentWindow>
    </main>
  )
}
