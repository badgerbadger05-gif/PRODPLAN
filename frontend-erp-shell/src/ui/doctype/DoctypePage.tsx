import { DocumentWindow } from '../layout/DocumentWindow'
import { AsyncState } from '../layout/AsyncState'
import { StatusBar } from '../layout/StatusBar'
import { CommandBar } from './CommandBar'
import { DoctypeTable } from './DoctypeTable'
import { FilterBar } from './FilterBar'
import { FormRenderer } from './FormRenderer'
import { SavedViewsBar } from './SavedViewsBar'
import {
  DialogHost,
  KeyboardShortcutShell,
  type DialogRegistry,
  type KeyboardShortcut,
} from '../platform'
import type { Doctype } from './types'
import type { DoctypeListState } from './useDoctypeList'
import type { AccessSubject } from './permissions'
import { canView, canViewField } from './permissions'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { decodeViewState, encodeViewState, type ViewFilterValue, type ViewState } from '../views'
import { buildDoctypeCsv, downloadCsv } from './csvExport'

type Props<Row, Filters extends object, Detail> = {
  doctype: Doctype<Row, Filters, Detail>
  state: DoctypeListState<Row, Filters, Detail>
  breadcrumbs?: string
  access: AccessSubject
  renderDetail?: (value: Detail | Row, state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderDialog?: (dialog: NonNullable<DoctypeListState<Row, Filters, Detail>['dialog']>, close: () => void) => ReactNode
  dialogRegistry?: DialogRegistry
  onRowDoubleClick?: (row: Row) => void
  onBack?: () => void
  renderFilters?: (state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderTopBadge?: (state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderCommandBar?: (state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderToolbarAfter?: (state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  renderTable?: (state: DoctypeListState<Row, Filters, Detail>) => ReactNode
  splitClassName?: string
  loadingLabel?: string
  emptyLabel?: string
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
  onBack,
  renderFilters,
  renderTopBadge,
  renderCommandBar,
  renderToolbarAfter,
  renderTable,
  splitClassName,
  loadingLabel = 'Загрузка данных…',
  emptyLabel = 'Нет данных',
}: Props<Row, Filters, Detail>) {
  const [searchParams, setSearchParams] = useSearchParams()
  const columnOptions = useMemo(
    () => doctype.columns
      .filter((column) => column.visible?.({
        filters: state.filters,
        listMeta: state.listMeta,
      }) !== false)
      .filter((column) => canViewField(doctype.permissions, column.key, access))
      .map(({ key, title }) => ({ key, title })),
    [access, doctype.columns, doctype.permissions, state.filters, state.listMeta],
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
  const exportCsv = useCallback(() => {
    if (!doctype.meta.exportCsv) return
    const config = typeof doctype.meta.exportCsv === 'object' ? doctype.meta.exportCsv : {}
    const rows = config.rows === 'current-page'
      ? state.rows
      : state.selection.length ? state.selection : state.rows
    const csv = buildDoctypeCsv({
      doctype,
      rows,
      visibleColumns,
      access,
      filters: state.filters,
      listMeta: state.listMeta,
    })
    const configuredName = typeof doctype.meta.exportCsv === 'object'
      ? doctype.meta.exportCsv.filename
      : undefined
    downloadCsv(csv, configuredName ?? `${doctype.meta.name}.csv`)
  }, [access, doctype, state.filters, state.listMeta, state.rows, state.selection, visibleColumns])
  const resourceShortcuts = useMemo<KeyboardShortcut[]>(() => [
    {
      id: `${doctype.meta.name}-reload`,
      keys: 'F5',
      scope: 'resource',
      allowInEditable: true,
      allowInInteractive: true,
      run: state.reload,
    },
    ...onRowDoubleClick ? [{
      id: `${doctype.meta.name}-open`,
      keys: 'Enter',
      scope: 'resource' as const,
      enabled: () => Boolean(state.activeRow) && !state.loading,
      run: () => {
        if (state.activeRow) onRowDoubleClick(state.activeRow)
      },
    }] : [],
    ...onBack ? [{
      id: `${doctype.meta.name}-back`,
      keys: 'Escape',
      scope: 'resource' as const,
      run: onBack,
    }] : [],
  ], [doctype.meta.name, onBack, onRowDoubleClick, state.activeRow, state.loading, state.reload])

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
      <KeyboardShortcutShell shortcuts={resourceShortcuts} />
      <div className="topLine">
        <div className="breadcrumbs">{breadcrumbs ?? doctype.meta.title}</div>
        <div className="runBadge">
          {renderTopBadge ? renderTopBadge(state) : `строк: ${state.paging.total}`}
        </div>
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
        {renderCommandBar
          ? renderCommandBar(state)
          : <CommandBar doctype={doctype} state={state} access={access} onExportCsv={exportCsv} />}
        {renderToolbarAfter?.(state)}
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
        {state.listLoading && <div className="srOnly" role="status" aria-live="polite">Загрузка...</div>}
        {state.error && state.rows.length > 0 && <div className="errorLine" role="alert">{state.error}</div>}
        {state.message && (
          <div className="successLine" role={state.listLoading ? undefined : 'status'}>
            {state.message}
          </div>
        )}
        <div className={[
          doctype.detail || renderDetail ? 'split' : '',
          splitClassName ?? '',
        ].filter(Boolean).join(' ') || undefined}>
          <AsyncState
            loading={state.listLoading}
            error={state.error}
            empty={state.rows.length === 0 && (
              state.listLoading
              || Boolean(state.error)
              || Boolean(renderTable)
            )}
            loadingLabel={loadingLabel}
            emptyLabel={emptyLabel}
            onRetry={state.reload}
            announce={false}
          >
            {renderTable
              ? renderTable(state)
              : (
                  <DoctypeTable
                    doctype={doctype}
                    state={state}
                    onRowDoubleClick={onRowDoubleClick}
                    visibleColumns={visibleColumns}
                    density={density}
                    access={access}
                  />
                )}
          </AsyncState>
          {(doctype.detail || renderDetail) && (
            <aside className="detailPane">
              {state.detailLoading && <div>Загрузка...</div>}
              {!state.detailLoading && detailValue && (
                renderDetail
                  ? renderDetail(detailValue, state)
                  : doctype.detail
                    ? <FormRenderer value={detailValue} layout={doctype.detail} access={access} permissions={doctype.permissions} />
                    : null
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
