import { useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { qty } from '../../lib/format'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { ResourceDetailPane } from './resources/ResourceDetailPane'
import { useResourceEditor } from './resources/useResourceEditor'

export function ResourcesPage() {
  const [search, setSearch] = useState('')
  const {
    active,
    addKind,
    allKinds,
    beginCreate,
    creating,
    error,
    form,
    kinds,
    load,
    loadDetails,
    loading,
    message,
    removeKind,
    rows,
    saveResource,
    saving,
    selectedKind,
    selectResource,
    setForm,
    setSelectedKind,
    stages,
  } = useResourceEditor()

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((row) => row.resource_name.toLowerCase().includes(q))
  }, [rows, search])

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

        {error && <div className="errorLine" role="alert">{error}</div>}
        {message && <div className="successLine" role="status">{message}</div>}

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
