import { useEffect, useState } from 'react'
import type { BindingReviewItem, BindingReviewLine } from '../../domain/workshopBindingReview'
import { reasonLabels } from '../../domain/workshopBindingReview'
import type { ProductionResource } from '../../domain/resources'
import { qty } from '../../lib/format'
import { addResourceProductionKind, listResources } from '../../services/resources'
import {
  assignLineWorkshop,
  listReviewItemLines,
} from '../../services/workshopBindingReview'
import { DoctypePage, useDoctypeList } from '../doctype'
import type { AccessSubject } from '../doctype/permissions'
import type { DoctypeListState } from '../doctype/useDoctypeList'
import { useOptionalSession } from '../session'
import {
  workshopBindingReviewDoctype,
  workshopReasonOrder,
  type WorkshopBindingReviewFilters,
} from './workshop-binding-review/workshopBindingReviewDoctype'

const transitionalAccess: AccessSubject = {
  roles: ['planner'],
  permissions: [],
}

type ReviewState = DoctypeListState<
  BindingReviewItem,
  WorkshopBindingReviewFilters,
  never
>

function ReviewFilters({ state }: { state: ReviewState }) {
  const counts = (state.listMeta.counts_by_reason ?? {}) as Record<string, number>

  function switchScope(scope: 'active' | 'catalog') {
    state.applyViewState({
      filters: { ...state.filters, scope, reasonCode: '' },
      sort: [],
    })
  }

  return (
    <div className="commandBar">
      <button
        className={state.filters.scope === 'active' ? 'primary' : ''}
        onClick={() => switchScope('active')}
      >
        В производстве
      </button>
      <button
        className={state.filters.scope === 'catalog' ? 'primary' : ''}
        onClick={() => switchScope('catalog')}
      >
        Весь справочник
      </button>
      <div className="barSeparator" />
      {workshopReasonOrder.map((code) => (
        <button
          key={code}
          className={state.filters.reasonCode === code ? 'primary' : ''}
          onClick={() => state.setFilter(
            'reasonCode',
            state.filters.reasonCode === code ? '' : code,
          )}
          title={reasonLabels[code]}
        >
          {reasonLabels[code]} ({counts[code] ?? 0})
        </button>
      ))}
      <div className="barSeparator" />
      <label className="inlineControl">
        <input
          placeholder="Поиск: наименование / артикул"
          value={state.filters.search}
          onChange={(event) => state.setFilter('search', event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') state.applyFilters()
          }}
        />
      </label>
      <button onClick={state.applyFilters} disabled={state.loading}>Обновить</button>
    </div>
  )
}

function ReviewDetail({
  active,
  scope,
  resources,
  reload,
}: {
  active: BindingReviewItem
  scope: WorkshopBindingReviewFilters['scope']
  resources: ProductionResource[]
  reload: () => void
}) {
  const [lines, setLines] = useState<BindingReviewLine[]>([])
  const [linesLoading, setLinesLoading] = useState(false)
  const [bindResourceId, setBindResourceId] = useState('')
  const [lineWorkshops, setLineWorkshops] = useState<Record<number, string>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    setLines([])
    setLineWorkshops({})
    setError('')
    setMessage('')
    setBindResourceId(active.suggested_resource_id ? String(active.suggested_resource_id) : '')
    if (scope !== 'active' && active.active_lines === 0) return
    setLinesLoading(true)
    listReviewItemLines(active.item_id)
      .then((data) => {
        if (!cancelled) setLines(data.rows ?? [])
      })
      .catch(() => {
        if (!cancelled) setLines([])
      })
      .finally(() => {
        if (!cancelled) setLinesLoading(false)
      })
    return () => { cancelled = true }
  }, [active, scope])

  async function bindKindToResource() {
    if (!active.production_kind_id || !bindResourceId) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await addResourceProductionKind(Number(bindResourceId), active.production_kind_id)
      setMessage(`Вид «${active.production_kind_name ?? ''}» привязан к участку. Список обновлён.`)
      reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
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
      reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      {error && <div className="errorLine" role="alert">{error}</div>}
      {message && <div className="successLine" role="status">{message}</div>}
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
          <select value={bindResourceId} onChange={(event) => setBindResourceId(event.target.value)}>
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

      {(active.reason_code === 'NO_PRODUCTION_KIND' || active.reason_code === 'NO_SPEC') && (
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
              <span>{line.order_number} · {qty(line.remaining_qty)} шт</span>
              <select
                value={lineWorkshops[line.product_id] ?? bindResourceId}
                onChange={(event) => setLineWorkshops((current) => ({
                  ...current,
                  [line.product_id]: event.target.value,
                }))}
              >
                <option value="">— участок —</option>
                {resources.map((resource) => (
                  <option key={resource.resource_id} value={resource.resource_id}>
                    {resource.resource_name}
                  </option>
                ))}
              </select>
              <button disabled={saving} onClick={() => void assignWorkshop(line)}>Назначить</button>
            </div>
          ))}
        </>
      )}
      {linesLoading && <div className="emptyDetail">Загрузка строк…</div>}
    </>
  )
}

export function WorkshopBindingReviewPage() {
  const session = useOptionalSession()
  const access = session?.user ?? transitionalAccess
  const state = useDoctypeList(workshopBindingReviewDoctype, { limit: 100, access })
  const [resources, setResources] = useState<ProductionResource[]>([])

  useEffect(() => {
    listResources().then(setResources).catch(() => setResources([]))
  }, [])

  return (
    <DoctypePage
      doctype={workshopBindingReviewDoctype}
      state={state}
      access={access}
      breadcrumbs="Производство / Разбор привязок к участкам"
      renderTopBadge={(current) => <>Проблемных деталей: {current.paging.total}</>}
      renderCommandBar={(current) => <ReviewFilters state={current} />}
      renderFilters={() => null}
      renderDetail={(value, current) => (
        <ReviewDetail
          active={value as BindingReviewItem}
          scope={current.filters.scope}
          resources={resources}
          reload={current.reload}
        />
      )}
    />
  )
}
