import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { listAssemblyQueue, type AssemblyQueueResponse } from '../../../services/productionControl'
import { AsyncState } from '../../layout/AsyncState'

export function AssemblyQueuePanel() {
  const [searchParams] = useSearchParams()
  const currentIdentity = searchParams.get('current_identity')?.trim() || null
  const [response, setResponse] = useState<AssemblyQueueResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      setResponse(await listAssemblyQueue({ current_identity: currentIdentity }))
    } catch (nextError) {
      if (signal?.aborted) return
      setResponse(null)
      setError(nextError instanceof Error ? nextError.message : String(nextError))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [currentIdentity])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  return (
    <section className="assemblyQueuePanel" aria-label="Текущая очередь сборки">
      <div className="commandBar">
        <button type="button" onClick={() => void load()} disabled={loading}>Обновить очередь</button>
        {response && <span className="toolbarText">Revision: {response.rows[0]?.source_revision ?? '—'}</span>}
      </div>
      {error && <div className="errorLine" role="alert">Очередь сборки недоступна: {error}</div>}
      <AsyncState
        loading={loading}
        error={error}
        empty={response?.rows.length === 0}
        loadingLabel="Загрузка очереди сборки…"
        emptyLabel={currentIdentity ? 'Строка текущей очереди не найдена' : 'Очередь сборки пуста'}
        onRetry={() => void load()}
      >
        {response && (
          <table className="productionOrdersTable assemblyQueueTable">
            <caption>Текущая очередь сборки</caption>
            <thead>
              <tr><th>Изделие</th><th>Период</th><th>Осталось</th><th>Identity</th></tr>
            </thead>
            <tbody>
              {response.rows.map((row) => (
                <tr
                  key={row.current_identity}
                  data-testid={`assembly-queue-row-${row.current_identity}`}
                  className={row.current_identity === currentIdentity ? 'activeRow' : undefined}
                >
                  <td>{row.item_name}<div className="muted">{row.item_code}</div></td>
                  <td>{row.period_from} — {row.period_to}</td>
                  <td>{row.assembly_remaining_qty}</td>
                  <td>
                    <code>{row.current_identity}</code>
                    <div className="muted">{row.source_revision}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </AsyncState>
    </section>
  )
}
