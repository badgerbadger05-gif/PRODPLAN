import { useEffect, useMemo, useState } from 'react'
import { syncActions, type NomenclatureGroupItem, type ODataConfig, type SyncAction, type WarehouseItem } from '../../domain/sync'
import {
  fetchODataMetadata,
  getODataConfig,
  listNomenclatureGroups,
  listWarehouses,
  saveNomenclatureGroupSelection,
  saveODataConfig,
  saveWarehouseSelection,
  testODataConnection,
} from '../../services/sync'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import {
  ConnectionPanel,
  FullSyncPanel,
  SelectionPanel,
  SyncActionGroups,
  SyncOperationLog,
  SyncProgress,
} from './sync/components'
import { useSyncRunner } from './sync/useSyncRunner'

const emptyConfig: ODataConfig = { base_url: '', username: '', password: '', token: '' }

export function SyncPage() {
  const [config, setConfig] = useState<ODataConfig>(emptyConfig)
  const [warehouses, setWarehouses] = useState<WarehouseItem[]>([])
  const [selectedWarehouses, setSelectedWarehouses] = useState<Set<string>>(new Set())
  const [groups, setGroups] = useState<NomenclatureGroupItem[]>([])
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set())

  const groupedActions = useMemo(() => {
    const map = new Map<string, SyncAction[]>()
    syncActions.forEach((action) => {
      if (!map.has(action.group)) map.set(action.group, [])
      map.get(action.group)!.push(action)
    })
    return Array.from(map.entries())
  }, [])

  async function loadConfig() {
    try {
      const data = await getODataConfig()
      setConfig({ ...emptyConfig, ...data })
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e))
    }
  }

  async function loadWarehouses() {
    try {
      const data = await listWarehouses()
      setWarehouses(data.rows ?? [])
      setSelectedWarehouses(new Set((data.rows ?? []).filter((row) => row.is_selected).map((row) => row.warehouse_ref1c)))
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e))
    }
  }

  async function loadGroups() {
    try {
      const data = await listNomenclatureGroups()
      const rows = data.items ?? data.rows ?? []
      setGroups(rows)
      setSelectedGroups(new Set(data.selected_ids ?? []))
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e))
    }
  }

  const {
    error,
    exportReport,
    log,
    message,
    progress,
    reportError,
    runAction,
    runFullSync,
    runNamed,
    running,
  } = useSyncRunner({
    config,
    refreshWarehouses: loadWarehouses,
    refreshSelections: async () => {
      await Promise.all([loadWarehouses(), loadGroups()])
    },
  })

  const busy = Boolean(running)
  const progressPercent = progress.total ? Math.round((progress.done / progress.total) * 100) : 0

  async function saveConfig() {
    await runNamed('Сохранить настройки', () => saveODataConfig(config))
  }

  useEffect(() => {
    void loadConfig()
    void loadWarehouses()
    void loadGroups()
    // Bootstrap is intentionally one-shot; later refreshes are explicit runner callbacks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
            <ConnectionPanel
              config={config}
              busy={busy}
              onConfigChange={setConfig}
              onSave={() => void saveConfig()}
              onTest={() => void runNamed('Тест подключения', () => testODataConnection(config))}
              onFetchMetadata={() => void runNamed('Выгрузить метаданные', () => fetchODataMetadata(config))}
            />

            <FullSyncPanel busy={busy} onRun={() => void runFullSync()} />

            <SyncProgress {...progress} percent={progressPercent} />

            {error && <div className="errorLine" role="alert">{error}</div>}
            {message && <div className="successLine" role="status">{message}</div>}

            <SyncActionGroups
              groups={groupedActions}
              busy={busy}
              onRunAction={(action) => void runAction(action)}
              onExport={(kind) => void exportReport(kind)}
            />
          </section>

          <aside className="syncSide">
            <SelectionPanel
              title="Склады для остатков"
              count={warehouses.length}
              selected={selectedWarehouses.size}
              busy={busy}
              onSelectAll={() => setSelectedWarehouses(new Set(warehouses.map((row) => row.warehouse_ref1c)))}
              onClear={() => setSelectedWarehouses(new Set())}
              onSave={() => void runNamed('Сохранить выбор складов', () => saveWarehouseSelection(Array.from(selectedWarehouses)))}
            >
              {warehouses.map((w) => (
                <label className="selectionRow" key={w.warehouse_ref1c}>
                  <input
                    type="checkbox"
                    disabled={busy}
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
              busy={busy}
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
                    disabled={busy}
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

          <SyncOperationLog entries={log} />
        </div>
      </DocumentWindow>
    </main>
  )
}
