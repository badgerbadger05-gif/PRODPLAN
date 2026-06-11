import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  BindingReviewItem,
  BindingReviewLine,
  BindingReviewReason,
} from '../../domain/workshopBindingReview'
import { reasonLabels, reasonPillClass } from '../../domain/workshopBindingReview'
import type { ProductionResource } from '../../domain/resources'
import { qty } from '../../lib/format'
import { addResourceProductionKind, listResources } from '../../services/resources'
import {
  assignLineWorkshop,
  listReviewItemLines,
  listReviewItems,
} from '../../services/workshopBindingReview'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'

const limit = 100
const reasonOrder: BindingReviewReason[] = [
  'NO_PRODUCTION_KIND',
  'KIND_NOT_BOUND',
  'NO_WAREHOUSE_BINDING',
  'NO_SPEC',
]

export function WorkshopBindingReviewPage() {
  const [scope, setScope] = useState<'active' | 'catalog'>('active')
  const [rows, setRows] = useState<BindingReviewItem[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [reasonFilter, setReasonFilter] = useState('')
  const [activeId, setActiveId] = useState<number | null>(null)
  const [lines, setLines] = useState<BindingReviewLine[]>([])
  const [linesLoading, setLinesLoading] = useState(false)
  const [resources, setResources] = useState<ProductionResource[]>([])
  const [bindResourceId, setBindResourceId] = useState('')
  const [lineWorkshops, setLineWorkshops] = useState<Record<number, string>>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const active = useMemo(
    () => rows.find((row) => row.item_id === activeId) ?? null,
    [rows, activeId],
  )

  const load = useCallback(
    async (nextOffset: number, nextScope?: 'active' | 'catalog', nextReason?: string) => {
      setLoading(true)
      setError('')
      try {
        const data = await listReviewItems({
          scope: nextScope ?? scope,
          search: search.trim() || undefined,
          reasonCode: (nextReason ?? reasonFilter) || undefined,
          limit,
          offset: nextOffset,
        })
        setRows(data.items ?? [])
        setCounts(data.counts_by_reason ?? {})
        setTotal(data.total ?? 0)
        setOffset(nextOffset)
        setActiveId((current) =>
          current && (data.items ?? []).some((row) => row.item_id === current)
            ? current
            : data.items?.[0]?.item_id ?? null,
        )
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    },
    [scope, search, reasonFilter],
  )

  useEffect(() => {
    void load(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    listResources()
      .then(setResources)
      .catch(() => setResources([]))
  }, [])

  useEffect(() => {
    setLines([])
    setLineWorkshops({})
    if (!active) return
    setBindResourceId(active.suggested_resource_id ? String(active.suggested_resource_id) : '')
    if (scope !== 'active' && active.active_lines === 0) return
    setLinesLoading(true)
    listReviewItemLines(active.item_id)
      .then((data) => setLines(data.rows ?? []))
      .catch(() => setLines([]))
      .finally(() => setLinesLoading(false))
  }, [active, scope])

  function switchScope(next: 'active' | 'catalog') {
    setScope(next)
    setReasonFilter('')
    void load(0, next, '')
  }

  function toggleReason(code: string) {
    const next = reasonFilter === code ? '' : code
    setReasonFilter(next)
    void load(0, undefined, next)
  }

  async function bindKindToResource() {
    if (!active?.production_kind_id || !bindResourceId) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await addResourceProductionKind(Number(bindResourceId), active.production_kind_id)
      setMessage(
        `Вид «${active.production_kind_name ?? ''}» привязан к участку. Список обновлён.`,
      )
      await load(offset)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function assignWorkshop(line: BindingReviewLine) {
    const workshopId = Number(lineWorkshops[line.product_id] || bindResourceId)
    if (!workshopId) {
      setError('Выберите участок для назначения строке.')
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await assignLineWorkshop(line.product_id, workshopId)
      setMessage(`Строке заказа ${line.order_number} назначен участок вручную.`)
      await load(offset)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const visibleFrom = total ? offset + 1 : 0
  const visibleTo = Math.min(offset + rows.length, total)

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Производство / Разбор привязок к участкам</div>
        <div className="runBadge">Проблемных деталей: {total}</div>
      </div>

      <DocumentWindow
        title="Разбор привязок"
        subtitle="Детали, не привязанные к участку автоматически (по виду производства)"
        hotkeys="F5 Обновить"
        footer={
          <StatusBar
            loading={loading}
            visibleFrom={visibleFrom}
            visibleTo={visibleTo}
            total={total}
            selectedCount={active ? 1 : 0}
            canPrev={offset > 0}
            canNext={offset + rows.length < total}
            onPrev={() => void load(Math.max(0, offset - limit))}
            onNext={() => void load(offset + limit)}
          />
        }
      >
        <div className="commandBar">
          <button
            className={scope === 'active' ? 'primary' : ''}
            onClick={() => switchScope('active')}
          >
            В производстве
          </button>
          <button
            className={scope === 'catalog' ? 'primary' : ''}
            onClick={() => switchScope('catalog')}
          >
            Весь справочник
          </button>
          <div className="barSeparator" />
          {reasonOrder.map((code) => (
            <button
              key={code}
              className={reasonFilter === code ? 'primary' : ''}
              onClick={() => toggleReason(code)}
              title={reasonLabels[code]}
            >
              {reasonLabels[code]} ({counts[code] ?? 0})
            </button>
          ))}
          <div className="barSeparator" />
          <label className="inlineControl">
            <input
              placeholder="Поиск: наименование / артикул"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void load(0)}
            />
          </label>
          <button onClick={() => void load(0)}>Обновить</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="split">
          <div className="tablePane">
            <table className="journalTable">
              <thead>
                <tr>
                  <th>Артикул</th>
                  <th>Наименование</th>
                  <th>Спецификация</th>
                  <th>Вид производства</th>
                  <th>Проблема</th>
                  {scope === 'active' && <th>Строк</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.item_id}
                    className={row.item_id === activeId ? 'activeRow' : ''}
                    onClick={() => setActiveId(row.item_id)}
                  >
                    <td>{row.item_article || row.item_code}</td>
                    <td className="itemCell">
                      <strong>{row.item_name}</strong>
                      <span>{row.item_code}</span>
                    </td>
                    <td>{row.spec_name || '—'}</td>
                    <td>{row.production_kind_name || '— не заполнен'}</td>
                    <td>
                      <span className={`pill ${reasonPillClass[row.reason_code]}`}>
                        {reasonLabels[row.reason_code]}
                      </span>
                    </td>
                    {scope === 'active' && (
                      <td className="numCell">
                        <strong>{qty(row.active_lines)}</strong>
                      </td>
                    )}
                  </tr>
                ))}
                {!rows.length && !loading && (
                  <tr>
                    <td colSpan={scope === 'active' ? 6 : 5}>
                      Все детали привязаны автоматически — разбирать нечего.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <aside className="detailPane">
            {active ? (
              <>
                <div className="detailTitle">{active.item_name}</div>
                <div className="detailMeta">
                  {active.item_article || active.item_code} · {active.spec_name || 'без спецификации'}
                </div>

                <h4>Проблема</h4>
                <p>{active.reason_text}</p>
                <h4>Рекомендация</h4>
                <p>{active.recommendation}</p>
                {active.suggested_resource_name && (
                  <p>
                    Подсказка по этапам: участок «{active.suggested_resource_name}»
                    {active.suggested_stage_name ? ` (этап «${active.suggested_stage_name}»)` : ''}.
                    Проверьте перед привязкой.
                  </p>
                )}

                {active.reason_code === 'KIND_NOT_BOUND' && active.production_kind_id && (
                  <div className="resourceKindAdder">
                    <h4>Привязать вид «{active.production_kind_name}» к участку</h4>
                    <select
                      value={bindResourceId}
                      onChange={(e) => setBindResourceId(e.target.value)}
                    >
                      <option value="">— выберите участок —</option>
                      {resources.map((resource) => (
                        <option key={resource.resource_id} value={resource.resource_id}>
                          {resource.resource_name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="primary"
                      disabled={saving || !bindResourceId}
                      onClick={() => void bindKindToResource()}
                    >
                      Привязать вид → участок
                    </button>
                  </div>
                )}

                {(active.reason_code === 'NO_PRODUCTION_KIND' ||
                  active.reason_code === 'NO_SPEC') && (
                  <p className="emptyDetail">
                    Исправляется в 1С: после правки выполните синхронизацию спецификаций на
                    странице «Синхронизация» и обновите этот список.
                  </p>
                )}
                {active.reason_code === 'NO_WAREHOUSE_BINDING' && (
                  <p className="emptyDetail">
                    Склад участка настраивается в настройках производственного контроля
                    (привязки «участок → склад»).
                  </p>
                )}

                {lines.length > 0 && (
                  <>
                    <h4>Активные строки заказов — назначить участок вручную</h4>
                    {lines.map((line) => (
                      <div className="resourceKindAdder" key={line.product_id}>
                        <span>
                          {line.order_number} · {qty(line.remaining_qty)} шт
                        </span>
                        <select
                          value={lineWorkshops[line.product_id] ?? bindResourceId}
                          onChange={(e) =>
                            setLineWorkshops((prev) => ({
                              ...prev,
                              [line.product_id]: e.target.value,
                            }))
                          }
                        >
                          <option value="">— участок —</option>
                          {resources.map((resource) => (
                            <option key={resource.resource_id} value={resource.resource_id}>
                              {resource.resource_name}
                            </option>
                          ))}
                        </select>
                        <button disabled={saving} onClick={() => void assignWorkshop(line)}>
                          Назначить
                        </button>
                      </div>
                    ))}
                  </>
                )}
                {linesLoading && <div className="emptyDetail">Загрузка строк…</div>}
              </>
            ) : (
              <div className="emptyDetail">Выберите деталь</div>
            )}
          </aside>
        </div>
      </DocumentWindow>
    </main>
  )
}
