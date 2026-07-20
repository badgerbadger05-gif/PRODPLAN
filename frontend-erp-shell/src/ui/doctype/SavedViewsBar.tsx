import { useEffect, useMemo, useRef, useState } from 'react'
import { LocalSavedViewsRepository, useSavedViews, type ViewState } from '../views'

type Props = {
  resource: string
  initialFilters: object
  filters: object
  sort: { sortBy: string; sortDir: 'asc' | 'desc' } | null
  columns: readonly { key: string; title: string }[]
  visibleColumns: readonly string[]
  density: 'compact' | 'comfortable'
  onApply: (state: ViewState) => void
  onVisibleColumnsChange: (columns: readonly string[]) => void
  onDensityChange: (density: 'compact' | 'comfortable') => void
}

export function SavedViewsBar(props: Props) {
  const repository = useMemo(() => new LocalSavedViewsRepository(), [])
  const initialState = useMemo<ViewState>(() => ({
    filters: props.initialFilters as ViewState['filters'],
    sort: [],
    visibleColumns: props.columns.map((column) => column.key),
    density: 'compact',
  }), [props.columns, props.initialFilters])
  const saved = useSavedViews(props.resource, initialState, repository)
  const setSavedState = saved.setState
  const [name, setName] = useState('')
  const [columnsOpen, setColumnsOpen] = useState(false)
  const defaultApplied = useRef<string | null>(null)
  const currentState = useMemo<ViewState>(() => ({
    filters: props.filters as ViewState['filters'],
    sort: props.sort ? [{ field: props.sort.sortBy, direction: props.sort.sortDir }] : [],
    visibleColumns: props.visibleColumns,
    density: props.density,
  }), [props.density, props.filters, props.sort, props.visibleColumns])

  useEffect(() => setSavedState(currentState), [currentState, setSavedState])
  useEffect(() => {
    if (saved.loading || !saved.activeViewId || defaultApplied.current === saved.activeViewId) return
    defaultApplied.current = saved.activeViewId
    props.onApply(saved.state)
  }, [props, saved.activeViewId, saved.loading, saved.state])

  const apply = (id: string) => {
    const view = saved.views.find((item) => item.id === id)
    saved.apply(view?.id ?? null)
    defaultApplied.current = view?.id ?? null
    setName(view?.name ?? '')
    props.onApply(view?.state ?? initialState)
  }

  const save = async () => {
    if (!name.trim()) return
    const view = await saved.save(name, {
      id: saved.activeViewId ?? undefined,
      state: currentState,
    })
    defaultApplied.current = view.id
  }

  const remove = async () => {
    if (!saved.activeViewId) return
    await saved.remove(saved.activeViewId)
    setName('')
    defaultApplied.current = null
    props.onApply(initialState)
  }

  const toggleColumn = (key: string) => {
    const next = props.visibleColumns.includes(key)
      ? props.visibleColumns.filter((column) => column !== key)
      : [...props.visibleColumns, key]
    if (next.length) props.onVisibleColumnsChange(next)
  }

  return (
    <div className="savedViewsBar" aria-label="Сохранённые представления">
      <label>
        <span>Вид</span>
        <select aria-label="Сохранённое представление" value={saved.activeViewId ?? ''} onChange={(event) => apply(event.target.value)}>
          <option value="">Стандартный</option>
          {saved.views.map((view) => (
            <option key={view.id} value={view.id}>{view.name}{view.id === saved.defaultViewId ? ' ★' : ''}</option>
          ))}
        </select>
      </label>
      <input aria-label="Название представления" value={name} placeholder="Название вида" onChange={(event) => setName(event.target.value)} />
      <button type="button" onClick={() => void save()} disabled={!name.trim()}>{saved.activeViewId ? 'Обновить' : 'Сохранить'}</button>
      <button type="button" disabled={!saved.activeViewId} onClick={() => void saved.setDefault(saved.defaultViewId === saved.activeViewId ? null : saved.activeViewId)}>
        {saved.activeViewId === saved.defaultViewId ? 'Не по умолчанию' : 'По умолчанию'}
      </button>
      <button type="button" disabled={!saved.activeViewId} onClick={() => void remove()}>Удалить</button>
      <button type="button" aria-expanded={columnsOpen} onClick={() => setColumnsOpen((value) => !value)}>Колонки</button>
      <label>
        <span>Плотность</span>
        <select aria-label="Плотность таблицы" value={props.density} onChange={(event) => props.onDensityChange(event.target.value as Props['density'])}>
          <option value="compact">Плотно</option>
          <option value="comfortable">Свободно</option>
        </select>
      </label>
      {columnsOpen && (
        <div className="savedViewsColumns">
          {props.columns.map((column) => (
            <label key={column.key}>
              <input type="checkbox" checked={props.visibleColumns.includes(column.key)} onChange={() => toggleColumn(column.key)} />
              {column.title}
            </label>
          ))}
        </div>
      )}
      {saved.error && <span className="errorLine">{saved.error}</span>}
    </div>
  )
}
