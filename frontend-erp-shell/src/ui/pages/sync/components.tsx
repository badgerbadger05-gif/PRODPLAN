import type { ReactNode } from 'react'
import type {
  ODataConfig,
  SyncAction,
  SyncLogEntry,
} from '../../../domain/sync'

type ConnectionPanelProps = {
  config: ODataConfig
  busy: boolean
  onConfigChange: (config: ODataConfig) => void
  onSave: () => void
  onTest: () => void
  onFetchMetadata: () => void
}

export function ConnectionPanel({
  config,
  busy,
  onConfigChange,
  onSave,
  onTest,
  onFetchMetadata,
}: ConnectionPanelProps) {
  return (
    <div className="syncPanel">
      <h2>Подключение</h2>
      <label><span>Базовый URL</span><input value={config.base_url} onChange={(e) => onConfigChange({ ...config, base_url: e.target.value })} /></label>
      <div className="syncFormGrid">
        <label><span>Пользователь</span><input value={config.username || ''} onChange={(e) => onConfigChange({ ...config, username: e.target.value })} /></label>
        <label><span>Пароль</span><input type="password" value={config.password || ''} onChange={(e) => onConfigChange({ ...config, password: e.target.value })} /></label>
        <label><span>Bearer token</span><input value={config.token || ''} onChange={(e) => onConfigChange({ ...config, token: e.target.value })} /></label>
      </div>
      <div className="syncActionsRow">
        <button className="primary" onClick={onSave} disabled={busy}>Сохранить настройки</button>
        <button onClick={onTest} disabled={busy}>Тест подключения</button>
        <button onClick={onFetchMetadata} disabled={busy}>Выгрузить метаданные</button>
      </div>
    </div>
  )
}

type FullSyncPanelProps = {
  busy: boolean
  onRun: () => void
}

export function FullSyncPanel({ busy, onRun }: FullSyncPanelProps) {
  return (
    <div className="syncPanel syncFull">
      <div>
        <h2>Полная синхронизация</h2>
        <p>Очередность: справочники, структура производства, склады, остатки, производственные и поставщицкие заказы.</p>
      </div>
      <button className="primary" onClick={onRun} disabled={busy}>Запустить полную синхронизацию</button>
    </div>
  )
}

type SyncProgressProps = {
  done: number
  total: number
  title: string
  percent: number
}

export function SyncProgress({ done, total, title, percent }: SyncProgressProps) {
  if (total <= 0) return null
  return (
    <div
      className="syncProgress"
      role="progressbar"
      aria-label={title}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      aria-valuetext={`${done} из ${total}`}
    >
      <div><strong>{title}</strong><span>{done} из {total} · {percent}%</span></div>
      <div className="progressTrack"><div style={{ width: `${percent}%` }} /></div>
    </div>
  )
}

type SyncActionGroupsProps = {
  groups: Array<[string, SyncAction[]]>
  busy: boolean
  onRunAction: (action: SyncAction) => void
  onExport: (kind: 'production' | 'supplier') => void
}

export function SyncActionGroups({
  groups,
  busy,
  onRunAction,
  onExport,
}: SyncActionGroupsProps) {
  return (
    <div className="syncGroups">
      {groups.map(([group, actions]) => (
        <div className="syncPanel" key={group}>
          <h2>{group}</h2>
          <div className="syncButtonGrid">
            {actions.map((action) => (
              <button key={action.id} onClick={() => onRunAction(action)} disabled={busy}>{action.title}</button>
            ))}
          </div>
        </div>
      ))}
      <div className="syncPanel">
        <h2>Excel-отчёты</h2>
        <div className="syncButtonGrid">
          <button onClick={() => onExport('production')} disabled={busy}>Заказы на производство</button>
          <button onClick={() => onExport('supplier')} disabled={busy}>Учитываемые заказы поставщику</button>
        </div>
      </div>
    </div>
  )
}

type SelectionPanelProps = {
  title: string
  count: number
  selected: number
  onSelectAll: () => void
  onClear: () => void
  onSave: () => void
  busy: boolean
  children: ReactNode
}

export function SelectionPanel({
  title,
  count,
  selected,
  onSelectAll,
  onClear,
  onSave,
  busy,
  children,
}: SelectionPanelProps) {
  return (
    <div className="syncPanel selectionPanel">
      <h2>{title}</h2>
      <div className="selectionMeta">Всего: {count} · Выбрано: {selected}</div>
      <div className="syncActionsRow">
        <button onClick={onSelectAll} disabled={busy}>Все</button>
        <button onClick={onClear} disabled={busy}>Снять</button>
        <button className="primary" onClick={onSave} disabled={busy}>Сохранить</button>
      </div>
      <div className="selectionList">{children || <div className="emptyDetail">Список пуст</div>}</div>
    </div>
  )
}

type SyncOperationLogProps = {
  entries: SyncLogEntry[]
}

export function SyncOperationLog({ entries }: SyncOperationLogProps) {
  return (
    <section className="syncLog">
      <h2>Журнал операций</h2>
      {entries.map((entry, index) => (
        <div className={`logRow ${entry.status}`} key={`${entry.at}-${index}`}>
          <strong>{entry.at}</strong>
          <span>{entry.title}</span>
          <em>{entry.status}</em>
          {entry.details && <small>{entry.details}</small>}
        </div>
      ))}
      {!entries.length && <div className="emptyDetail">Операций пока не было</div>}
    </section>
  )
}
