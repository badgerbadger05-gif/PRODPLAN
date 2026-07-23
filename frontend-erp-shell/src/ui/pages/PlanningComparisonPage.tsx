import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  CutoffGrade,
  DiffClassification,
  PlanningComparisonBatch,
  PlanningComparisonBatchDetail,
} from '../../domain/planningComparison'
import {
  capturePlanningComparison,
  getPlanningComparisonBatch,
  listPlanningComparisonBatches,
} from '../../services/planningComparison'

const cutoffLabels: Record<CutoffGrade, string> = {
  exact: 'Точный срез',
  near: 'Близкий срез',
  invalid: 'Несопоставимый срез',
}

const classificationLabels: Record<DiffClassification, string> = {
  equal: 'Совпало',
  changed: 'Изменилось',
  stable_only: 'Только стабильный',
  shadow_only: 'Только параллельный',
}

function CutoffBadge({ grade }: { grade: CutoffGrade }) {
  return <span className={`comparisonBadge comparisonBadge-${grade}`}>{cutoffLabels[grade]}</span>
}

function formatDate(value: string) {
  return value ? new Date(value).toLocaleString('ru-RU') : '—'
}

export function PlanningComparisonPage() {
  const [batches, setBatches] = useState<PlanningComparisonBatch[]>([])
  const [selected, setSelected] = useState<PlanningComparisonBatchDetail | null>(null)
  const [classification, setClassification] = useState<'all' | DiffClassification>('all')
  const [loading, setLoading] = useState(true)
  const [capturing, setCapturing] = useState(false)
  const [error, setError] = useState('')

  const selectBatch = useCallback(async (id: number, signal?: AbortSignal) => {
    const detail = await getPlanningComparisonBatch(id, signal)
    setSelected(detail)
  }, [])

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      const result = await listPlanningComparisonBatches(50, 0, signal)
      setBatches(result.rows)
      if (result.rows.length) await selectBatch(result.rows[0].id, signal)
      else setSelected(null)
    } catch (reason) {
      if (!signal?.aborted) setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [selectBatch])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function capture() {
    setCapturing(true)
    setError('')
    try {
      const detail = await capturePlanningComparison()
      setSelected(detail)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setCapturing(false)
    }
  }

  const diffs = useMemo(
    () => (selected?.diffs ?? []).filter((row) => classification === 'all' || row.classification === classification),
    [classification, selected],
  )
  const kindMetrics = Object.entries(selected?.metrics.by_kind ?? {})

  return (
    <main className="workArea comparisonPage">
      <header className="comparisonHeader">
        <div>
          <h1>Сравнение планирования</h1>
          <p>Стабильный и параллельный контуры на сопоставимых входных данных</p>
        </div>
        <button className="primary" disabled={capturing} onClick={() => void capture()}>
          {capturing ? 'Снимаем…' : 'Снять сравнение'}
        </button>
      </header>
      <div className="comparisonSafetyNote">
        Снимок использует последние готовые результаты. Он не запускает планирование и ничего не записывает в 1С.
      </div>
      {error && <div className="errorLine" role="alert">{error}</div>}
      <div className="comparisonLayout">
        <section className="comparisonBatches">
          <h2>Снимки</h2>
          {loading && <div className="hintLine">Загрузка сравнений…</div>}
          {!loading && !batches.length && <div className="hintLine">Сравнений пока нет</div>}
          {batches.map((batch) => (
            <button
              key={batch.id}
              className={`comparisonBatch${selected?.id === batch.id ? ' active' : ''}`}
              onClick={() => void selectBatch(batch.id).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))}
            >
              <span>{formatDate(batch.created_at)}</span>
              <CutoffBadge grade={batch.cutoff_grade} />
            </button>
          ))}
        </section>
        <section className="comparisonDetail">
          {!selected && !loading && <div className="hintLine">Выберите или снимите сравнение</div>}
          {selected && (
            <>
              <div className="comparisonSummary">
                <div><span>Срез</span><CutoffBadge grade={selected.cutoff_grade} /></div>
                <div><span>Строк</span><strong>{selected.metrics.rows ?? selected.diffs.length}</strong></div>
                <div><span>Стабильный прогон</span><strong>{selected.stable_run_key ?? 'нет готового прогона'}</strong></div>
                <div><span>Параллельный прогон</span><strong>{selected.shadow_run_key ?? 'нет готового прогона'}</strong></div>
              </div>
              {selected.cutoff_reason && <div className="comparisonReason">{selected.cutoff_reason}</div>}
              <div className="comparisonMetrics">
                {kindMetrics.map(([kind, metric]) => (
                  <article key={kind}>
                    <strong>{kind}</strong>
                    <span>Совпало: {metric.equal}</span>
                    <span>Изменилось: {metric.changed}</span>
                    <span>Только стабильный: {metric.stable_only}</span>
                    <span>Только параллельный: {metric.shadow_only}</span>
                    <span>Σ отклонений: {metric.absolute_delta}</span>
                  </article>
                ))}
              </div>
              <div className="comparisonToolbar">
                <label>Показать
                  <select value={classification} onChange={(event) => setClassification(event.target.value as typeof classification)}>
                    <option value="all">Все строки</option>
                    {Object.entries(classificationLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                </label>
                <span>{diffs.length} строк</span>
              </div>
              <div className="comparisonTableWrap">
                <table className="comparisonTable">
                  <thead><tr><th>Тип</th><th>Позиция</th><th>Стабильный</th><th>Параллельный</th><th>Δ</th><th>Результат</th></tr></thead>
                  <tbody>
                    {diffs.map((row) => (
                      <tr key={`${row.result_kind}:${row.canonical_key}`}>
                        <td>{row.result_kind}</td><td title={row.canonical_key}>{row.item_key}</td>
                        <td className="num">{row.stable_quantity}</td><td className="num">{row.shadow_quantity}</td>
                        <td className="num">{row.delta_quantity}</td><td>{classificationLabels[row.classification]}</td>
                      </tr>
                    ))}
                    {!diffs.length && <tr><td colSpan={6} className="hintLine">Нет строк для выбранного фильтра</td></tr>}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>
      </div>
    </main>
  )
}
