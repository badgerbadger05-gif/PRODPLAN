import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { fullSyncOrder, syncActions, type NomenclatureGroupItem, type ODataConfig, type SyncAction, type SyncLogEntry, type WarehouseItem } from '../../domain/sync'
import { downloadBase64File } from '../../lib/download'
import {
  exportProductionOrdersReport,
  exportSupplierOrdersReport,
  fetchODataMetadata,
  getODataConfig,
  listNomenclatureGroups,
  listWarehouses,
  runSyncAction,
  saveNomenclatureGroupSelection,
  saveODataConfig,
  saveWarehouseSelection,
  testODataConnection,
} from '../../services/sync'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const emptyConfig: ODataConfig = { base_url: '', username: '', password: '', token: '' }

function shortResult(value: unknown) {
  try {
    return JSON.stringify(value).slice(0, 260)
  } catch {
    return String(value).slice(0, 260)
  }
}

function nowTime() {
  return new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function SyncPage() {
  const [config, setConfig] = useState<ODataConfig>(emptyConfig)
  const [warehouses, setWarehouses] = useState<WarehouseItem[]>([])
  const [selectedWarehouses, setSelectedWarehouses] = useState<Set<string>>(new Set())
  const [groups, setGroups] = useState<NomenclatureGroupItem[]>([])
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set())
  const [running, setRunning] = useState<string>('')
  const [log, setLog] = useState<SyncLogEntry[]>([])
  const [progress, setProgress] = useState({ done: 0, total: 0, title: '' })
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const groupedActions = useMemo(() => {
    const map = new Map<string, SyncAction[]>()
    syncActions.forEach((action) => {
      if (!map.has(action.group)) map.set(action.group, [])
      map.get(action.group)!.push(action)
    })
    return Array.from(map.entries())
  }, [])

  const busy = Boolean(running)
  const progressPercent = progress.total ? Math.round((progress.done / progress.total) * 100) : 0

  function addLog(entry: Omit<SyncLogEntry, 'at'>) {
    setLog((current) => [{ ...entry, at: nowTime() }, ...current].slice(0, 40))
  }

  async function loadConfig() {
    try {
      const data = await getODataConfig()
      setConfig({ ...emptyConfig, ...data })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function loadWarehouses() {
    try {
      const data = await listWarehouses()
      setWarehouses(data.rows ?? [])
      setSelectedWarehouses(new Set((data.rows ?? []).filter((row) => row.is_selected).map((row) => row.warehouse_ref1c)))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function loadGroups() {
    try {
      const data = await listNomenclatureGroups()
      const rows = data.items ?? data.rows ?? []
      setGroups(rows)
      setSelectedGroups(new Set(data.selected_ids ?? []))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function runNamed(title: string, runner: () => Promise<unknown>) {
    setRunning(title)
    setError('')
    setMessage('')
    addLog({ title, status: 'running' })
    try {
      const result = await runner()
      addLog({ title, status: 'ok', details: shortResult(result) })
      setMessage(`${title}: выполнено`)
      return result
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e)
      addLog({ title, status: 'error', details: text })
      setError(text)
      throw e
    } finally {
      setRunning('')
    }
  }

  async function runAction(action: SyncAction) {
    await runNamed(action.title, () => runSyncAction(config, action))
    if (action.id === 'warehouses') await loadWarehouses()
  }

  async function runFullSync() {
    setRunning('Полная синхронизация')
    setError('')
    setMessage('')
    setProgress({ done: 0, total: fullSyncOrder.length, title: 'Полная синхронизация' })
    try {
      for (const id of fullSyncOrder) {
        const action = syncActions.find((item) => item.id === id)
        if (!action) continue
        addLog({ title: action.title, status: 'running' })
        const result = await runSyncAction(config, action)
        addLog({ title: action.title, status: 'ok', details: shortResult(result) })
        setProgress((current) => ({ ...current, done: current.done + 1 }))
      }
      setMessage('Полная синхронизация завершена')
      await Promise.all([loadWarehouses(), loadGroups()])
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e)
      setError(text)
      addLog({ title: 'Полная синхронизация', status: 'error', details: text })
    } finally {
      setRunning('')
    }
  }

  async function saveConfig() {
    await runNamed('Сохранить настройки', () => saveODataConfig(config))
  }

  async function exportReport(kind: 'production' | 'supplier') {
    await runNamed(kind === 'production' ? 'Excel: заказы на производство' : 'Excel: заказы поставщику', async () => {
      const result = kind === 'production' ? await exportProductionOrdersReport() : await exportSupplierOrdersReport()
      downloadBase64File(result, kind === 'production' ? 'production_orders.xlsx' : 'supplier_orders.xlsx')
      return result
    })
  }

  useEffect(() => {
    void loadConfig()
    void loadWarehouses()
    void loadGroups()
  }, [])

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Данные / Синхронизация 1С OData</div>
        <div className="runBadge">{busy ? running : 'Готово'}</div>
      </div>

      <DocumentWindow
        title="Синхронизация 1С OData"
        subtitle="Настройки подключения, регламентные загрузки, выбор складов и диагностические Excel-отчёты"
        hotkeys="Последовательность полной синхронизации зафиксирована в интерфейсе"
        footer={(
          <StatusBar
            loading={busy}
            visibleFrom={log.length ? 1 : 0}
            visibleTo={log.length}
            total={log.length}
            selectedCount={selectedWarehouses.size}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <div className="syncLayout">
          <section className="syncMain">
            <div className="syncPanel">
              <h2>Подключение</h2>
              <label><span>Базовый URL</span><input value={config.base_url} onChange={(e) => setConfig({ ...config, base_url: e.target.value })} /></label>
              <div className="syncFormGrid">
                <label><span>Пользователь</span><input value={config.username || ''} onChange={(e) => setConfig({ ...config, username: e.target.value })} /></label>
                <label><span>Пароль</span><input type="password" value={config.password || ''} onChange={(e) => setConfig({ ...config, password: e.target.value })} /></label>
                <label><span>Bearer token</span><input value={config.token || ''} onChange={(e) => setConfig({ ...config, token: e.target.value })} /></label>
              </div>
              <div className="syncActionsRow">
                <button className="primary" onClick={() => void saveConfig()} disabled={busy}>Сохранить настройки</button>
                <button onClick={() => void runNamed('Тест подключения', () => testODataConnection(config))} disabled={busy}>Тест подключения</button>
                <button onClick={() => void runNamed('Выгрузить метаданные', () => fetchODataMetadata(config))} disabled={busy}>Выгрузить метаданные</button>
              </div>
            </div>

            <div className="syncPanel syncFull">
              <div>
                <h2>Полная синхронизация</h2>
                <p>Очередность: справочники, структура производства, склады, остатки, производственные и поставщицкие заказы.</p>
              </div>
              <button className="primary" onClick={() => void runFullSync()} disabled={busy}>Запустить полную синхронизацию</button>
            </div>

            {progress.total > 0 && (
              <div className="syncProgress">
                <div><strong>{progress.title}</strong><span>{progress.done} из {progress.total} · {progressPercent}%</span></div>
                <div className="progressTrack"><div style={{ width: `${progressPercent}%` }} /></div>
              </div>
            )}

            {error && <div className="errorLine">{error}</div>}
            {message && <div className="successLine">{message}</div>}

            <div className="syncGroups">
              {groupedActions.map(([group, actions]) => (
                <div className="syncPanel" key={group}>
                  <h2>{group}</h2>
                  <div className="syncButtonGrid">
                    {actions.map((action) => (
                      <button key={action.id} onClick={() => void runAction(action)} disabled={busy}>{action.title}</button>
                    ))}
                  </div>
                </div>
              ))}
              <div className="syncPanel">
                <h2>Excel-отчёты</h2>
                <div className="syncButtonGrid">
                  <button onClick={() => void exportReport('production')} disabled={busy}>Заказы на производство</button>
                  <button onClick={() => void exportReport('supplier')} disabled={busy}>Учитываемые заказы поставщику</button>
                </div>
              </div>
            </div>
          </section>

          <aside className="syncSide">
            <SelectionPanel
              title="Склады для остатков"
              count={warehouses.length}
              selected={selectedWarehouses.size}
              onSelectAll={() => setSelectedWarehouses(new Set(warehouses.map((row) => row.warehouse_ref1c)))}
              onClear={() => setSelectedWarehouses(new Set())}
              onSave={() => void runNamed('Сохранить выбор складов', () => saveWarehouseSelection(Array.from(selectedWarehouses)))}
            >
              {warehouses.map((w) => (
                <label className="selectionRow" key={w.warehouse_ref1c}>
                  <input
                    type="checkbox"
                    checked={selectedWarehouses.has(w.warehouse_ref1c)}
                    onChange={(e) => {
                      const next = new Set(selectedWarehouses)
                      if (e.target.checked) next.add(w.warehouse_ref1c)
                      else next.delete(w.warehouse_ref1c)
                      setSelectedWarehouses(next)
                    }}
                  />
                  <span>{w.warehouse_code ? `${w.warehouse_code} — ` : ''}{w.warehouse_name}</span>
                </label>
              ))}
            </SelectionPanel>

            <SelectionPanel
              title="Группы номенклатуры"
              count={groups.length}
              selected={selectedGroups.size}
              onSelectAll={() => setSelectedGroups(new Set(groups.map((row) => row.id)))}
              onClear={() => setSelectedGroups(new Set())}
              onSave={() => void runNamed('Сохранить выбор групп', () => saveNomenclatureGroupSelection(Array.from(selectedGroups)))}
            >
              {groups.length === 0 ? (
                <div className="emptyDetail" style={{ padding: '8px 0' }}>
                  {selectedGroups.size > 0
                    ? `Список групп не загружен. Сохранено ${selectedGroups.size} позиций — запустите синхронизацию «Группы номенклатуры», чтобы обновить список.`
                    : 'Список групп пуст — запустите синхронизацию «Группы номенклатуры».'}
                </div>
              ) : groups.map((g) => (
                <label className="selectionRow" key={g.id}>
                  <input
                    type="checkbox"
                    checked={selectedGroups.has(g.id)}
                    onChange={(e) => {
                      const next = new Set(selectedGroups)
                      if (e.target.checked) next.add(g.id)
                      else next.delete(g.id)
                      setSelectedGroups(next)
                    }}
                  />
                  <span>{g.code ? `${g.code} — ` : ''}{g.name}</span>
                </label>
              ))}
            </SelectionPanel>
          </aside>

          <section className="syncLog">
            <h2>Журнал операций</h2>
            {log.map((entry, index) => (
              <div className={`logRow ${entry.status}`} key={`${entry.at}-${index}`}>
                <strong>{entry.at}</strong>
                <span>{entry.title}</span>
                <em>{entry.status}</em>
                {entry.details && <small>{entry.details}</small>}
              </div>
            ))}
            {!log.length && <div className="emptyDetail">Операций пока не было</div>}
          </section>
        </div>
      </DocumentWindow>
    </main>
  )
}

function SelectionPanel({ title, count, selected, onSelectAll, onClear, onSave, children }: {
  title: string
  count: number
  selected: number
  onSelectAll: () => void
  onClear: () => void
  onSave: () => void
  children: ReactNode
}) {
  return (
    <div className="syncPanel selectionPanel">
      <h2>{title}</h2>
      <div className="selectionMeta">Всего: {count} · Выбрано: {selected}</div>
      <div className="syncActionsRow">
        <button onClick={onSelectAll}>Все</button>
        <button onClick={onClear}>Снять</button>
        <button className="primary" onClick={onSave}>Сохранить</button>
      </div>
      <div className="selectionList">{children || <div className="emptyDetail">Список пуст</div>}</div>
    </div>
  )
}
