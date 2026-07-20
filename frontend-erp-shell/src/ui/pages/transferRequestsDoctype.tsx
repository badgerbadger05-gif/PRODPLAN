import {
  type MaterialIssueDetail,
  type TransferIssueRow,
  type TransferIssuesResponse,
} from '../../domain/productionControl'
import { dateRu, qty } from '../../lib/format'
import {
  deleteMaterialIssue,
  getMaterialIssue,
  listMaterialIssues,
  markMaterialIssueAssembled,
} from '../../services/productionControl'
import type { Doctype } from '../doctype'

export type TransferRequestFilters = {
  search: string
  status: string
  sourceWarehouseRef: string
}

export const transferStatusLabels: Record<string, string> = {
  draft: 'Черновик',
  requested: 'Заявка',
  exported: 'В 1С',
  posted: 'Собрано',
  error: 'Ошибка',
  cancelled: 'Отменено',
}

export function warehouseLabel(name?: string | null, ref?: string | null) {
  return name || ref || '—'
}

export function orderMainLine(row: TransferIssueRow) {
  return row.order_prodplan_number || row.order_number
}

export function orderSubline(row: TransferIssueRow) {
  if (row.order_ref1c) return row.order_one_c_number || row.order_number
  return 'заказ не в 1С'
}

function canAssemble(row: TransferIssueRow | null) {
  return row
    ? (row.can_assemble ?? (!!row.exported_ref1c && row.status !== 'posted'))
    : false
}

function assembleDisabledReason(row: TransferIssueRow | null) {
  return row?.assemble_disabled_reason
    || (!row
      ? 'Выберите заявку'
      : !row.exported_ref1c
        ? 'Сначала выгрузите перемещение в 1С'
        : row.status === 'posted'
          ? 'Перемещение уже собрано'
          : '')
}

function canDelete(row: TransferIssueRow | null) {
  return Boolean(row && !row.exported_ref1c && !row.one_c_number)
}

export const transferRequestsDoctype: Doctype<
  TransferIssueRow,
  TransferRequestFilters,
  MaterialIssueDetail
> = {
  meta: {
    name: 'material_transfer',
    title: 'Заявки на перемещение',
    subtitle: 'Непроведённые перемещения из запуска заказов и детали комплектующих к сборке',
    hotkeys: 'F5 Обновить · Enter Детали',
    idField: 'issue_id',
    selectionMode: 'single',
  },
  initialFilters: { search: '', status: '', sourceWarehouseRef: '' },
  dataSource: {
    async list({ limit, offset, filters }) {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
      })
      if (filters.status) params.set('status', filters.status)
      if (filters.sourceWarehouseRef) params.set('source_warehouse_ref1c', filters.sourceWarehouseRef)
      if (filters.search.trim()) params.set('search', filters.search.trim())
      return listMaterialIssues(params)
    },
    detail: (id) => getMaterialIssue(Number(id)),
  },
  columns: [
    { key: 'select', title: '', className: 'checkCol', width: 32, minWidth: 32, align: 'center', type: 'select-checkbox' },
    {
      key: 'issue',
      title: 'Заявка',
      className: 'orderCell',
      minWidth: 116,
      autoWidth: true,
      render: (row) => <><strong>{row.document_number}</strong><span>{dateRu(row.created_at) || '—'} · строк {row.lines_count ?? 0}</span></>,
    },
    {
      key: 'order',
      title: 'Заказ',
      className: 'orderCell transferOrderCell',
      minWidth: 142,
      autoWidth: true,
      render: (row) => <><strong>{orderMainLine(row)}</strong><span>{orderSubline(row)}</span></>,
    },
    {
      key: 'item',
      title: 'Деталь',
      className: 'itemCell',
      minWidth: 300,
      grow: true,
      render: (row) => <><strong>{row.item_name}</strong><span>{row.item_article || row.item_code || ''}</span></>,
    },
    {
      key: 'quantity',
      title: 'Кол-во',
      className: 'numCell',
      minWidth: 64,
      autoWidth: true,
      render: (row) => <><strong>{qty(row.remaining_qty || row.quantity)}</strong><span>{row.unit || ''}</span></>,
    },
    {
      key: 'source_warehouse',
      title: 'Склад',
      minWidth: 150,
      autoWidth: true,
      value: (row) => warehouseLabel(row.source_warehouse_name, row.source_warehouse_ref1c),
    },
    {
      key: 'one_c',
      title: '1С',
      minWidth: 128,
      autoWidth: true,
      render: (row) => (
        <>
          <strong>{row.one_c_number || (row.exported_ref1c ? row.exported_ref1c.slice(0, 8) : '—')}</strong>
          <span>{row.one_c_number && row.exported_ref1c ? row.exported_ref1c.slice(0, 8) : dateRu(row.exported_at) || row.export_error || ''}</span>
        </>
      ),
    },
    {
      key: 'status',
      title: 'Статус',
      minWidth: 92,
      autoWidth: true,
      render: (row) => (
        <span className={`pill ${row.status === 'posted' ? 'assembled' : row.status === 'error' ? 'shortage' : 'to_move'}`}>
          {transferStatusLabels[row.status] || row.status}
        </span>
      ),
    },
  ],
  filters: [
    { kind: 'search', field: 'search', mode: 'submit' },
    {
      kind: 'select',
      field: 'sourceWarehouseRef',
      label: 'Склад',
      allowEmpty: true,
      options: (meta) => ((meta.source_warehouses ?? []) as NonNullable<TransferIssuesResponse['source_warehouses']>)
        .map((warehouse) => ({
          value: warehouse.warehouse_ref1c,
          label: warehouseLabel(warehouse.warehouse_name, warehouse.warehouse_ref1c),
        })),
    },
    {
      kind: 'select',
      field: 'status',
      label: 'Статус',
      allowEmpty: true,
      options: Object.entries(transferStatusLabels)
        .filter(([value]) => value !== 'cancelled')
        .map(([value, label]) => ({ value, label })),
    },
  ],
  actions: [
    {
      key: 'assembled',
      label: 'Собрано',
      scope: 'row',
      tone: 'primary',
      enabled: ({ activeRow }) => canAssemble(activeRow),
      disabledReason: ({ activeRow }) => assembleDisabledReason(activeRow),
      async run({ activeRow }) {
        if (!activeRow) return {}
        await markMaterialIssueAssembled(activeRow.issue_id)
        return {
          message: `Перемещение ${activeRow.one_c_number || activeRow.document_number} проведено, обеспечение обновлено: собрано`,
          reload: true,
        }
      },
    },
    {
      key: 'delete',
      label: 'Удалить',
      scope: 'row',
      tone: 'danger',
      enabled: ({ activeRow }) => canDelete(activeRow),
      disabledReason: () => 'Можно удалить только заявку без 1С',
      confirm: ({ activeRow }) => `Удалить локальную заявку ${activeRow?.document_number ?? ''}?`,
      async run({ activeRow }) {
        if (!activeRow) return {}
        await deleteMaterialIssue(activeRow.issue_id)
        return { message: `Удалена локальная заявка ${activeRow.document_number}`, reload: true }
      },
    },
  ],
  detail: {
    sections: [{
      title: 'Комплектующие',
      table: {
        rows: (detail) => 'lines' in detail ? detail.lines : [],
        columns: [
          { key: 'item_name', title: 'Комплектующее', type: 'text' },
          { key: 'required_qty', title: 'Нужно', type: 'qty' },
          { key: 'issued_qty', title: 'Выдано', type: 'qty' },
          { key: 'line_status', title: 'Статус', type: 'status' },
        ],
      },
    }],
  },
  permissions: {
    view: ['viewer', 'shopfloor', 'planner', 'admin'],
    actions: {
      assembled: 'material_issue.assemble_post_1c',
      delete: 'production.propose',
    },
  },
  renderExtraToolbar: ({ activeRow }) => {
    const reason = assembleDisabledReason(activeRow)
    return !canAssemble(activeRow) && reason ? <span className="toolbarText">{reason}</span> : null
  },
}
