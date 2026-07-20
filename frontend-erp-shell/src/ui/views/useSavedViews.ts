import { useCallback, useEffect, useMemo, useState } from 'react'
import { cloneViewState, type SavedView, type SavedViewsRepository, type ViewState } from './types'

export interface SavedViewsController {
  views: readonly SavedView[]
  activeViewId: string | null
  defaultViewId: string | null
  state: ViewState
  loading: boolean
  error: string | null
  setState: (state: ViewState) => void
  apply: (id: string | null) => void
  save: (name: string, options?: { id?: string; makeDefault?: boolean }) => Promise<SavedView>
  remove: (id: string) => Promise<void>
  setDefault: (id: string | null) => Promise<void>
  reload: () => Promise<void>
}

export function useSavedViews(
  resource: string,
  initialState: ViewState,
  repository: SavedViewsRepository,
): SavedViewsController {
  const initialStateKey = JSON.stringify(initialState)
  const resourceInitialState = useMemo(
    () => JSON.parse(initialStateKey) as ViewState,
    [initialStateKey],
  )
  const [views, setViews] = useState<readonly SavedView[]>([])
  const [activeViewId, setActiveViewId] = useState<string | null>(null)
  const [defaultViewId, setDefaultViewId] = useState<string | null>(null)
  const [state, setStateValue] = useState(() => cloneViewState(resourceInitialState))
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [nextViews, nextDefaultId] = await Promise.all([
        repository.list(resource),
        repository.getDefaultId(resource),
      ])
      setViews(nextViews)
      setDefaultViewId(nextDefaultId)
      const defaultView = nextViews.find((view) => view.id === nextDefaultId)
      if (defaultView) {
        setActiveViewId(defaultView.id)
        setStateValue(cloneViewState(defaultView.state))
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось загрузить представления')
    } finally {
      setLoading(false)
    }
  }, [repository, resource])

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    Promise.all([repository.list(resource), repository.getDefaultId(resource)])
      .then(([nextViews, nextDefaultId]) => {
        if (!active) return
        setViews(nextViews)
        setDefaultViewId(nextDefaultId)
        const defaultView = nextViews.find((view) => view.id === nextDefaultId)
        setActiveViewId(defaultView?.id ?? null)
        setStateValue(cloneViewState(defaultView?.state ?? resourceInitialState))
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить представления')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [repository, resource, resourceInitialState])

  const apply = useCallback((id: string | null) => {
    const view = views.find((item) => item.id === id)
    setActiveViewId(view?.id ?? null)
    setStateValue(cloneViewState(view?.state ?? resourceInitialState))
  }, [resourceInitialState, views])

  const save = useCallback(async (
    name: string,
    options: { id?: string; makeDefault?: boolean } = {},
  ) => {
    setError(null)
    try {
      const view = await repository.save({
        resource,
        name,
        state,
        id: options.id,
        makeDefault: options.makeDefault,
      })
      const nextViews = await repository.list(resource)
      setViews(nextViews)
      setActiveViewId(view.id)
      if (options.makeDefault) setDefaultViewId(view.id)
      return view
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Не удалось сохранить представление'
      setError(message)
      throw reason
    }
  }, [repository, resource, state])

  const remove = useCallback(async (id: string) => {
    await repository.remove(resource, id)
    const [nextViews, nextDefaultId] = await Promise.all([
      repository.list(resource),
      repository.getDefaultId(resource),
    ])
    setViews(nextViews)
    setDefaultViewId(nextDefaultId)
    if (activeViewId === id) {
      setActiveViewId(null)
      setStateValue(cloneViewState(resourceInitialState))
    }
  }, [activeViewId, repository, resource, resourceInitialState])

  const setDefault = useCallback(async (id: string | null) => {
    await repository.setDefaultId(resource, id)
    setDefaultViewId(id)
  }, [repository, resource])

  const setState = useCallback((nextState: ViewState) => {
    setStateValue(cloneViewState(nextState))
  }, [])

  return useMemo(() => ({
    views,
    activeViewId,
    defaultViewId,
    state,
    loading,
    error,
    setState,
    apply,
    save,
    remove,
    setDefault,
    reload,
  }), [activeViewId, apply, defaultViewId, error, loading, reload, remove, save, setDefault, setState, state, views])
}
