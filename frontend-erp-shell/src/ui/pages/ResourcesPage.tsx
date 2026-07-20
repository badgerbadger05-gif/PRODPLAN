import { useCallback, useEffect, useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import type { ProductionKind, ProductionResource, ProductionResourcePayload, ResourceProductionKind, ResourceStage } from '../../domain/resources'
import { qty } from '../../lib/format'
import {
  addResourceProductionKind,
  createResource,
  listProductionKinds,
  listResourceProductionKinds,
  listResources,
  listResourceStages,
  removeResourceProductionKind,
  updateResource,
} from '../../services/resources'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import {
  emptyResourceForm,
  normalizeResourcePayload,
  resourceToForm,
} from './resources/resourceForm'
import { ResourceDetailPane } from './resources/ResourceDetailPane'

export function ResourcesPage() {
  const [rows, setRows] = useState<ProductionResource[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [stages, setStages] = useState<ResourceStage[]>([])
  const [kinds, setKinds] = useState<ResourceProductionKind[]>([])
  const [allKinds, setAllKinds] = useState<ProductionKind[]>([])
  const [selectedKind, setSelectedKind] = useState('')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState<ProductionResourcePayload>(emptyResourceForm)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((row) => row.resource_name.toLowerCase().includes(q))
  }, [rows, search])

  const active = useMemo(() => creating ? null : rows.find((row) => row.resource_id === activeId) ?? filtered[0] ?? null, [rows, filtered, activeId, creating])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await listResources()
      setRows(data)
      setActiveId((current) => current && data.some((row) => row.resource_id === current) ? current : data[0]?.resource_id ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadProductionKindsCatalog = useCallback(async () => {
    try {
      setAllKinds(await listProductionKinds())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const loadDetails = useCallback(async (resource: ProductionResource) => {
    if (creating) return
    setActiveId(resource.resource_id)
    setForm(resourceToForm(resource))
    setStages([])
    setKinds([])
    try {
      const [nextStages, nextKinds] = await Promise.all([
        listResourceStages(resource.resource_id),
        listResourceProductionKinds(resource.resource_id),
      ])
      setStages(nextStages)
      setKinds(nextKinds)
      setSelectedKind('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [creating])

  function beginCreate() {
    setCreating(true)
    setActiveId(null)
    setStages([])
    setKinds([])
    setForm(emptyResourceForm())
    setError('')
    setMessage('')
  }

  function selectResource(resource: ProductionResource) {
    setCreating(false)
    setActiveId(resource.resource_id)
    setForm(resourceToForm(resource))
  }

  function handleResourceKeyDown(event: KeyboardEvent<HTMLTableRowElement>, index: number) {
    if (event.target !== event.currentTarget) return

    let nextIndex = index
    if (event.key === 'ArrowDown') nextIndex = Math.min(index + 1, filtered.length - 1)
    else if (event.key === 'ArrowUp') nextIndex = Math.max(index - 1, 0)
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = filtered.length - 1
    else if (event.key === 'Enter') {
      if (filtered[index]?.resource_id !== active?.resource_id) selectResource(filtered[index])
      return
    } else {
      return
    }

    event.preventDefault()
    const nextResource = filtered[nextIndex]
    if (!nextResource) return
    if (nextResource.resource_id !== active?.resource_id) selectResource(nextResource)
    event.currentTarget.parentElement
      ?.querySelector<HTMLTableRowElement>(`tr[data-resource-id="${nextResource.resource_id}"]`)
      ?.focus()
  }

  async function saveResource() {
    const payload = normalizeResourcePayload(form)
    if (!payload.resource_name) {
      setError('Введите название участка')
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const saved = creating
        ? await createResource(payload)
        : active
          ? await updateResource(active.resource_id, payload)
          : null
      await load()
      if (saved) {
        setCreating(false)
        setActiveId(saved.resource_id)
        setForm(resourceToForm(saved))
        setMessage(creating ? 'Участок создан' : 'Участок сохранен')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function addKind() {
    if (!active || !selectedKind) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await addResourceProductionKind(active.resource_id, Number(selectedKind))
      await loadDetails(active)
      setMessage('Вид производства привязан к участку')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function removeKind(kind: ResourceProductionKind) {
    if (!active) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await removeResourceProductionKind(active.resource_id, kind.production_kind_id)
      await loadDetails(active)
      setMessage('Привязка вида производства снята')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => {
    void load()
    void loadProductionKindsCatalog()
  }, [load, loadProductionKindsCatalog])

  useEffect(() => {
    if (active) void loadDetails(active)
  }, [active, loadDetails])

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Настройки производства / Производственные ресурсы</div>
        <div className="runBadge">Участков: {rows.length}</div>
      </div>

      <DocumentWindow
        title="Производственные ресурсы"
        subtitle="Участки, мощности и привязки видов производства"
        hotkeys="Enter Сохранить · F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={filtered.length ? 1 : 0}
            visibleTo={filtered.length}
            total={filtered.length}
            selectedCount={active ? 1 : 0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="commandBar">
          <button className="primary" onClick={beginCreate}>Добавить участок</button>
          <button onClick={() => void saveResource()} disabled={saving || loading || (!creating && !active)}>Сохранить карточку</button>
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
          <div className="barSeparator" />
          <label className="inlineControl resourceSearch">
            <span>Поиск</span>
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="участок" />
          </label>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split">
          <div className="tablePane">
            <table className="journalTable resourcesTable">
              <thead>
                <tr>
                  <th>Участок</th>
                  <th>Мощность</th>
                  <th>Часов/сутки</th>
                  <th>График</th>
                  <th>Буфер</th>
                  <th>Сдвиг</th>
                  <th>Диапазон</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row, index) => (
                  <tr
                    key={row.resource_id}
                    data-resource-id={row.resource_id}
                    className={row.resource_id === active?.resource_id ? 'activeRow' : ''}
                    tabIndex={row.resource_id === active?.resource_id ? 0 : -1}
                    aria-selected={row.resource_id === active?.resource_id}
                    onClick={() => selectResource(row)}
                    onDoubleClick={() => void loadDetails(row)}
                    onKeyDown={(event) => handleResourceKeyDown(event, index)}
                  >
                    <td className="itemCell">
                      <strong>{row.resource_name}</strong>
                      <span>ID {row.resource_id}</span>
                    </td>
                    <td className="numCell"><strong>{qty(row.capacity)}</strong></td>
                    <td className="numCell"><strong>{qty(row.daily_work_hours)}</strong></td>
                    <td>{row.work_schedule || '—'}</td>
                    <td className="numCell"><strong>{qty(row.buffer_days)}</strong><span>дн.</span></td>
                    <td className="numCell"><strong>{qty(row.shift_offset)}</strong></td>
                    <td className="numCell"><strong>{qty(row.planning_range)}</strong><span>дн.</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ResourceDetailPane
            active={active}
            creating={creating}
            form={form}
            onFormChange={setForm}
            onSave={() => void saveResource()}
            saving={saving}
            kinds={kinds}
            allKinds={allKinds}
            selectedKind={selectedKind}
            onSelectedKindChange={setSelectedKind}
            onAddKind={() => void addKind()}
            onRemoveKind={(kind) => void removeKind(kind)}
            stages={stages}
          />
        </div>
      </DocumentWindow>
    </main>
  )
}
