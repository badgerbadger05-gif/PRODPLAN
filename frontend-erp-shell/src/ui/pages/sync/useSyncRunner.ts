import { useRef, useState } from 'react'
import type { ODataConfig, SyncAction, SyncLogEntry } from '../../../domain/sync'
import { fullSyncOrder, syncActions } from '../../../domain/sync'
import { downloadBase64File } from '../../../lib/download'
import {
  exportProductionOrdersReport,
  exportSupplierOrdersReport,
  runSyncAction,
} from '../../../services/sync'

const sensitiveKey = /password|token|authorization|secret/i

function shortResult(value: unknown) {
  try {
    return JSON.stringify(value, (key, item) => sensitiveKey.test(key) ? '[REDACTED]' : item).slice(0, 260)
  } catch {
    return String(value).slice(0, 260)
  }
}

function nowTime() {
  return new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

type UseSyncRunnerOptions = {
  config: ODataConfig
  refreshWarehouses: () => Promise<void>
  refreshSelections: () => Promise<void>
}

export function useSyncRunner({
  config,
  refreshWarehouses,
  refreshSelections,
}: UseSyncRunnerOptions) {
  const [running, setRunning] = useState('')
  const [log, setLog] = useState<SyncLogEntry[]>([])
  const [progress, setProgress] = useState({ done: 0, total: 0, title: '' })
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const locked = useRef(false)

  function addLog(entry: Omit<SyncLogEntry, 'at'>) {
    setLog((current) => [{ ...entry, at: nowTime() }, ...current].slice(0, 40))
  }

  async function runNamed(title: string, runner: () => Promise<unknown>) {
    if (locked.current) return false
    locked.current = true
    setRunning(title)
    setError('')
    setMessage('')
    addLog({ title, status: 'running' })
    try {
      const result = await runner()
      addLog({ title, status: 'ok', details: shortResult(result) })
      setMessage(`${title}: выполнено`)
      return true
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e)
      addLog({ title, status: 'error', details: text })
      setError(text)
      return false
    } finally {
      locked.current = false
      setRunning('')
    }
  }

  async function runAction(action: SyncAction) {
    const succeeded = await runNamed(action.title, () => runSyncAction(config, action))
    if (succeeded && action.id === 'warehouses') await refreshWarehouses()
  }

  async function runFullSync() {
    if (locked.current) return
    locked.current = true
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
      await refreshSelections()
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e)
      setError(text)
      addLog({ title: 'Полная синхронизация', status: 'error', details: text })
    } finally {
      locked.current = false
      setRunning('')
    }
  }

  async function exportReport(kind: 'production' | 'supplier') {
    await runNamed(kind === 'production' ? 'Excel: заказы на производство' : 'Excel: заказы поставщику', async () => {
      const result = kind === 'production' ? await exportProductionOrdersReport() : await exportSupplierOrdersReport()
      downloadBase64File(result, kind === 'production' ? 'production_orders.xlsx' : 'supplier_orders.xlsx')
      return result
    })
  }

  return {
    error,
    exportReport,
    log,
    message,
    progress,
    reportError: setError,
    runAction,
    runFullSync,
    runNamed,
    running,
  }
}
