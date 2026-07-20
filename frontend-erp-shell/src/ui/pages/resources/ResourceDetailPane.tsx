import type {
  ProductionKind,
  ProductionResource,
  ProductionResourcePayload,
  ResourceProductionKind,
  ResourceStage,
} from '../../../domain/resources'
import {
  availableKinds,
  productionKindName,
} from './resourceForm'

type ResourceDetailPaneProps = {
  active: ProductionResource | null
  creating: boolean
  form: ProductionResourcePayload
  onFormChange: (form: ProductionResourcePayload) => void
  onSave: () => void
  saving: boolean
  kinds: ResourceProductionKind[]
  allKinds: ProductionKind[]
  selectedKind: string
  onSelectedKindChange: (value: string) => void
  onAddKind: () => void
  onRemoveKind: (kind: ResourceProductionKind) => void
  stages: ResourceStage[]
}

export function ResourceDetailPane({
  active,
  creating,
  form,
  onFormChange,
  onSave,
  saving,
  kinds,
  allKinds,
  selectedKind,
  onSelectedKindChange,
  onAddKind,
  onRemoveKind,
  stages,
}: ResourceDetailPaneProps) {
  return (
    <aside className="detailPane">
      <h2>Карточка участка</h2>
      {active || creating ? (
        <>
          <div className="detailTitle">{creating ? 'Новый участок' : active?.resource_name}</div>
          <div className="detailMeta">{creating ? 'Создание карточки' : `ID ${active?.resource_id}`}</div>
          <ResourceForm
            form={form}
            onChange={onFormChange}
            onSave={onSave}
            saving={saving}
            creating={creating}
          />

          {!creating && (
            <>
              <h3>Виды производства</h3>
              <div className="tagList">
                {kinds.map((kind) => (
                  <span className="resourceTag removableTag" key={kind.id}>
                    {kind.production_kind_name || productionKindName(allKinds, kind.production_kind_id)}
                    <button
                      aria-label={`Удалить вид производства ${kind.production_kind_name || productionKindName(allKinds, kind.production_kind_id)}`}
                      onClick={() => onRemoveKind(kind)}
                      disabled={saving}
                    >
                      x
                    </button>
                  </span>
                ))}
                {!kinds.length && <div className="emptyDetail">Нет привязок видов производства</div>}
              </div>
              <div className="resourceKindAdder">
                <select
                  aria-label="Добавить вид производства"
                  value={selectedKind}
                  onChange={(event) => onSelectedKindChange(event.target.value)}
                >
                  <option value="">Добавить вид производства</option>
                  {availableKinds(allKinds, kinds).map((kind) => (
                    <option key={kind.id} value={kind.id}>{kind.name}</option>
                  ))}
                </select>
                <button onClick={onAddKind} disabled={!selectedKind || saving}>Добавить</button>
              </div>

              <h3>Этапы</h3>
              <div className="tagList">
                {stages.map((stage) => (
                  <span className="resourceTag" key={stage.id}>
                    {stage.stage_name || `Этап ${stage.stage_id}`}
                  </span>
                ))}
                {!stages.length && <div className="emptyDetail">Нет привязок этапов</div>}
              </div>
            </>
          )}
        </>
      ) : (
        <div className="emptyDetail">Выберите участок</div>
      )}
    </aside>
  )
}

export function ResourceForm({
  form,
  onChange,
  onSave,
  saving,
  creating,
}: {
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
        <input value={form.resource_name} onChange={(event) => onChange({ ...form, resource_name: event.target.value })} onKeyDown={(event) => event.key === 'Enter' && onSave()} />
      </label>
      <label>
        <span>Мощность</span>
        <input type="number" value={form.capacity ?? 0} onChange={(event) => onChange({ ...form, capacity: Number(event.target.value) })} />
      </label>
      <label>
        <span>Часов/сутки</span>
        <input type="number" value={form.daily_work_hours ?? 8} onChange={(event) => onChange({ ...form, daily_work_hours: Number(event.target.value) })} />
      </label>
      <label>
        <span>График</span>
        <select value={form.work_schedule ?? '5/2'} onChange={(event) => onChange({ ...form, work_schedule: event.target.value })}>
          {['5/2', '2/2', '6/1', '7/0', 'Сменный 24/7', 'Гибкий'].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <label>
        <span>Буфер, дней</span>
        <input type="number" value={form.buffer_days ?? 0} onChange={(event) => onChange({ ...form, buffer_days: Number(event.target.value) })} />
      </label>
      <label>
        <span>Сдвиг планирования</span>
        <input type="number" value={form.shift_offset ?? 0} onChange={(event) => onChange({ ...form, shift_offset: Number(event.target.value) })} />
      </label>
      <label>
        <span>Диапазон, дней</span>
        <input type="number" value={form.planning_range ?? 30} onChange={(event) => onChange({ ...form, planning_range: Number(event.target.value) })} />
      </label>
      <div className="detailActions">
        <button className="primary" onClick={onSave} disabled={saving}>{creating ? 'Создать' : 'Сохранить'}</button>
      </div>
    </div>
  )
}
