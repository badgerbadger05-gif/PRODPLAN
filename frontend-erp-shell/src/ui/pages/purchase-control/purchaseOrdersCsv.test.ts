import { describe, expect, it } from 'vitest'
import type { PurchaseRow } from '../../../domain/purchaseControl'
import { buildDoctypeCsv } from '../../doctype/csvExport'
import { createPurchaseOrdersDoctype } from './purchaseOrdersDoctype'

const row: PurchaseRow = {
  row_key: 'mrp:11',
  line_id: null,
  purchase_id: 11,
  source_purchase_ids: [12],
  order_id: null,
  order_number: '',
  order_date: '2026-07-20',
  order_ref1c: null,
  order_state_name: null,
  source: 'mrp',
  supplier_id: 7,
  supplier_name: 'ООО Металл',
  item_id: 42,
  item_code: 'CODE-42',
  item_article: 'ART-42',
  item_name: 'Лист стальной',
  unit: 'кг',
  quantity: 100,
  received_qty: 0,
  remaining_qty: 100,
  delivery_date: null,
  need_date: '2026-07-25',
  overdue_days: 0,
  line_status: 'to_order',
  supply_phase: 'no_goods',
  counts_in_mrp: true,
  price: 50,
  amount: 0,
  run_id: 3,
}

describe('purchase journal CSV schema', () => {
  it('keeps the established 14 columns, semicolon format, and value mapping', () => {
    const doctype = createPurchaseOrdersDoctype()
    const csv = buildDoctypeCsv({
      doctype,
      rows: [row],
      // The purchase export schema is intentionally independent of table visibility.
      visibleColumns: ['order'],
      access: { roles: ['buyer'], permissions: [] },
    })

    expect(csv).toBe(
      '"Заказ";"Дата заказа";"Поставщик";"Артикул";"Номенклатура";"Заказано";"Поступило";"Осталось";"Дата поставки";"Просрочка, дн";"Статус 1С";"Фаза";"Статус";"Сумма"\n'
      + '"MRP #11, MRP #12";"2026-07-20";"ООО Металл";"ART-42";"Лист стальной";"100";"0";"100";"2026-07-25";"";"";"Нет товара";"К заказу";""\n',
    )
  })

  it('does not export either field derived from an RBAC-hidden composite item column', () => {
    const doctype = createPurchaseOrdersDoctype()
    doctype.permissions = {
      ...doctype.permissions,
      fields: {
        item: 'purchase.item.view',
      },
    }

    const csv = buildDoctypeCsv({
      doctype,
      rows: [row],
      visibleColumns: doctype.columns.map((column) => column.key),
      access: { roles: ['buyer'], permissions: [] },
    })

    expect(csv).not.toContain('"Артикул"')
    expect(csv).not.toContain('"Номенклатура"')
    expect(csv).not.toContain('"ART-42"')
    expect(csv).not.toContain('"Лист стальной"')
  })

  it('does not export sibling fields derived from other RBAC-hidden composite columns', () => {
    const doctype = createPurchaseOrdersDoctype()
    doctype.permissions = {
      ...doctype.permissions,
      fields: {
        order: 'purchase.order.view',
        quantity: 'purchase.quantity.view',
        delivery_date: 'purchase.delivery.view',
        state: 'purchase.state.view',
      },
    }
    const orderedRow: PurchaseRow = {
      ...row,
      row_key: 'order-line:52',
      line_id: 52,
      purchase_id: null,
      source_purchase_ids: [],
      order_id: 8,
      order_number: 'ЗП-000008',
      order_state_name: 'К поступлению',
      remaining_qty: 37,
      delivery_date: '2026-07-24',
      overdue_days: 5,
      line_status: 'overdue',
      supply_phase: 'in_transit',
    }

    const csv = buildDoctypeCsv({
      doctype,
      rows: [orderedRow],
      visibleColumns: doctype.columns.map((column) => column.key),
      access: { roles: ['buyer'], permissions: [] },
    })

    expect(csv).not.toContain('"Дата заказа"')
    expect(csv).not.toContain('"Осталось"')
    expect(csv).not.toContain('"Просрочка, дн"')
    expect(csv).not.toContain('"Фаза"')
    expect(csv).not.toContain('"ЗП-000008"')
    expect(csv).not.toContain('"37"')
    expect(csv).not.toContain('"5"')
    expect(csv).not.toContain('"Товар в пути"')
  })
})
