import { useEffect, useMemo, useState } from 'react'
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

const emptyForm: ProductionResourcePayload = {
  resource_name: '',
  shift_offset: 0,
  planning_range: 30,
  capacity: 0,
  work_schedule: '5/2',
  daily_work_hours: 8,
  buffer_days: 0,
}

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
  const [form, setForm] = useState<ProductionResourcePayload>(emptyForm)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((row) => row.resource_name.toLowerCase().includes(q))
  }, [rows, search])

  const active = useMemo(() => creating ? null : rows.find((row) => row.resource_id === activeId) ?? filtered[0] ?? null, [rows, filtered, activeId, creating])

  async function load() {
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
  }

  async function loadProductionKindsCatalog() {
    try {
      setAllKinds(await listProductionKinds())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function loadDetails(resource: ProductionResource) {
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
  }

  function beginCreate() {
    setCreating(true)
    setActiveId(null)
    setStages([])
    setKinds([])
    setForm(emptyForm)
    setError('')
    setMessage('')
  }

  function selectResource(resource: ProductionResource) {
    setCreating(false)
    setActiveId(resource.resource_id)
    setForm(resourceToForm(resource))
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
  }, [])

  useEffect(() => {
    if (active) void loadDetails(active)
  }, [active?.resource_id])

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
                {filtered.map((row) => (
                  <tr key={row.resource_id} className={row.resource_id === active?.resource_id ? 'activeRow' : ''} onClick={() => selectResource(row)} onDoubleClick={() => void loadDetails(row)}>
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

          <aside className="detailPane">
            <h2>Карточка участка</h2>
            {active || creating ? (
              <>
                <div className="detailTitle">{creating ? 'Новый участок' : active?.resource_name}</div>
                <div className="detailMeta">{creating ? 'Создание карточки' : `ID ${active?.resource_id}`}</div>
                <ResourceForm form={form} onChange={setForm} onSave={() => void saveResource()} saving={saving} creating={creating} />

                {!creating && (
                  <>
                    <h3>Виды производства</h3>
                    <div className="tagList">
                      {kinds.map((kind) => (
                        <span className="resourceTag removableTag" key={kind.id}>
                          {kind.production_kind_name || productionKindName(allKinds, kind.production_kind_id)}
                          <button onClick={() => void removeKind(kind)} disabled={saving}>x</button>
                        </span>
                      ))}
                      {!kinds.length && <div className="emptyDetail">Нет привязок видов производства</div>}
                    </div>
                    <div className="resourceKindAdder">
                      <select value={selectedKind} onChange={(e) => setSelectedKind(e.target.value)}>
                        <option value="">Добавить вид производства</option>
                        {availableKinds(allKinds, kinds).map((kind) => <option key={kind.id} value={kind.id}>{kind.name}</option>)}
                      </select>
                      <button onClick={() => void addKind()} disabled={!selectedKind || saving}>Добавить</button>
                    </div>

                    <h3>Этапы</h3>
                    <div className="tagList">
                      {stages.map((stage) => <span className="resourceTag" key={stage.id}>{stage.stage_name || `Этап ${stage.stage_id}`}</span>)}
                      {!stages.length && <div className="emptyDetail">Нет привязок этапов</div>}
                    </div>
                  </>
                )}
              </>
            ) : (
              <div className="emptyDetail">Выберите участок</div>
            )}
          </aside>
        </div>
      </DocumentWindow>
    </main>
  )
}

function ResourceForm({ form, onChange, onSave, saving, creating }: {
  form: ProductionResourcePayload
  onChange: (form: ProductionResourcePayload) => void
  onSave: () => void
  saving: boolean
  creating: boolean
}) {
  return (
    <div className="resourceForm">
      <label>
        <span>Название участка</span>
        <input value={form.resource_name} onChange={(e) => onChange({ ...form, resource_name: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && onSave()} />
      </label>
      <label>
        <span>Мощность</span>
        <input type="number" value={form.capacity ?? 0} onChange={(e) => onChange({ ...form, capacity: Number(e.target.value) })} />
      </label>
      <label>
        <span>Часов/сутки</span>
        <input type="number" value={form.daily_work_hours ?? 8} onChange={(e) => onChange({ ...form, daily_work_hours: Number(e.target.value) })} />
      </label>
      <label>
        <span>График</span>
        <select value={form.work_schedule ?? '5/2'} onChange={(e) => onChange({ ...form, work_schedule: e.target.value })}>
          {['5/2', '2/2', '6/1', '7/0', 'Сменный 24/7', 'Гибкий'].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <label>
        <span>Буфер, дней</span>
        <input type="number" value={form.buffer_days ?? 0} onChange={(e) => onChange({ ...form, buffer_days: Number(e.target.value) })} />
      </label>
      <label>
        <span>Сдвиг планирования</span>
        <input type="number" value={form.shift_offset ?? 0} onChange={(e) => onChange({ ...form, shift_offset: Number(e.target.value) })} />
      </label>
      <label>
        <span>Диапазон, дней</span>
        <input type="number" value={form.planning_range ?? 30} onChange={(e) => onChange({ ...form, planning_range: Number(e.target.value) })} />
      </label>
      <div className="detailActions">
        <button className="primary" onClick={onSave} disabled={saving}>{creating ? 'Создать' : 'Сохранить'}</button>
      </div>
    </div>
  )
}

function resourceToForm(resource: ProductionResource): ProductionResourcePayload {
  return {
    resource_name: resource.resource_name || '',
    shift_offset: Number(resource.shift_offset ?? 0),
    planning_range: Number(resource.planning_range ?? 30),
    capacity: Number(resource.capacity ?? 0),
    work_schedule: resource.work_schedule || '5/2',
    daily_work_hours: Number(resource.daily_work_hours ?? 8),
    buffer_days: Number(resource.buffer_days ?? 0),
  }
}

function normalizeResourcePayload(form: ProductionResourcePayload): ProductionResourcePayload {
  return {
    resource_name: String(form.resource_name || '').trim(),
    shift_offset: Number(form.shift_offset ?? 0),
    planning_range: Number(form.planning_range ?? 30),
    capacity: Number(form.capacity ?? 0),
    work_schedule: form.work_schedule || '5/2',
    daily_work_hours: Number(form.daily_work_hours ?? 8),
    buffer_days: Number(form.buffer_days ?? 0),
  }
}

function productionKindName(kinds: ProductionKind[], id: number) {
  return kinds.find((kind) => kind.id === id)?.name || `ID ${id}`
}

function availableKinds(kinds: ProductionKind[], assigned: ResourceProductionKind[]) {
  const assignedIds = new Set(assigned.map((kind) => kind.production_kind_id))
  return kinds
    .filter((kind) => kind.id > 0 && !assignedIds.has(kind.id))
    .sort((a, b) => a.name.localeCompare(b.name, 'ru'))
}
