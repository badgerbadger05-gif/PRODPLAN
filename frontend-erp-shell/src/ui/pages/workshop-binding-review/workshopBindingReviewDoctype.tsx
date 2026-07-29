import {
  reasonLabels,
  reasonPillClass,
  type BindingReviewItem,
  type BindingReviewReason,
} from '../../../domain/workshopBindingReview'
import { qty } from '../../../lib/format'
import { listReviewItems } from '../../../services/workshopBindingReview'
import type { Doctype } from '../../doctype'

export type WorkshopBindingReviewFilters = {
  scope: 'active' | 'catalog'
  search: string
  reasonCode: '' | BindingReviewReason
}

export const workshopReasonOrder: BindingReviewReason[] = [
  'NO_PRODUCTION_KIND',
  'KIND_NOT_BOUND',
  'NO_WAREHOUSE_BINDING',
  'NO_SPEC',
]

export const workshopBindingReviewDoctype: Doctype<
  BindingReviewItem,
  WorkshopBindingReviewFilters,
  never
> = {
  meta: {
    name: 'workshop_binding',
    title: 'Разбор привязок',
    subtitle: 'Детали, не привязанные к участку автоматически (по виду производства)',
    hotkeys: 'F5 Обновить',
    idField: 'item_id',
    selectionMode: 'single',
    exportCsv: { filename: 'workshop_binding_review.csv' },
    emptyLabel: 'Все детали привязаны автоматически — разбирать нечего.',
  },
  initialFilters: {
    scope: 'active',
    search: '',
    reasonCode: '',
  },
  dataSource: {
    async list({ limit, offset, filters }) {
      const data = await listReviewItems({
        scope: filters.scope,
        search: filters.search.trim() || undefined,
        reasonCode: filters.reasonCode || undefined,
        limit,
        offset,
      })
      return {
        rows: data.items ?? [],
        total: data.total ?? 0,
        limit: data.limit,
        offset: data.offset,
        scope: data.scope,
        counts_by_reason: data.counts_by_reason ?? {},
      }
    },
  },
  columns: [
    {
      key: 'article',
      title: 'Артикул',
      minWidth: 90,
      autoWidth: true,
      value: (row) => row.item_article || row.item_code,
    },
    {
      key: 'item',
      title: 'Наименование',
      className: 'itemCell',
      minWidth: 190,
      width: 230,
      render: (row) => <><strong>{row.item_name}</strong><span>{row.item_code}</span></>,
    },
    {
      key: 'spec',
      title: 'Спецификация',
      minWidth: 110,
      autoWidth: true,
      value: (row) => row.spec_name || '—',
    },
    {
      key: 'production_kind',
      title: 'Вид производства',
      minWidth: 140,
      autoWidth: true,
      value: (row) => row.production_kind_name || '— не заполнен',
    },
    {
      key: 'reason',
      title: 'Проблема',
      minWidth: 160,
      autoWidth: true,
      render: (row) => (
        <span className={`pill ${reasonPillClass[row.reason_code]}`}>
          {reasonLabels[row.reason_code]}
        </span>
      ),
    },
    {
      key: 'active_lines',
      title: 'Строк',
      className: 'numCell',
      width: 60,
      type: 'qty',
      visible: ({ filters }) => filters.scope === 'active',
      render: (row) => <strong>{qty(row.active_lines)}</strong>,
    },
  ],
  filters: [
    {
      kind: 'select',
      field: 'scope',
      label: 'Область',
      options: [
        { value: 'active', label: 'В производстве' },
        { value: 'catalog', label: 'Весь справочник' },
      ],
    },
    {
      kind: 'select',
      field: 'reasonCode',
      label: 'Проблема',
      allowEmpty: true,
      options: workshopReasonOrder.map((value) => ({ value, label: reasonLabels[value] })),
    },
    {
      kind: 'search',
      field: 'search',
      placeholder: 'Поиск: наименование / артикул',
      mode: 'submit',
    },
  ],
  detail: { sections: [] },
  permissions: {
    view: ['planner', 'admin'],
  },
}
