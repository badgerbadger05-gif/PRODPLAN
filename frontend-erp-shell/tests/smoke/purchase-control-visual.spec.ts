import { expect, test, type Page } from '@playwright/test'

const buyRow = {
  row_key: 'buy:9:default',
  line_id: null,
  purchase_id: null,
  source_purchase_ids: [],
  order_id: null,
  order_number: '',
  order_date: null,
  order_ref1c: null,
  order_state_name: null,
  source: 'mrp',
  supplier_id: 7,
  supplier_name: 'Промснаб',
  item_id: 9,
  item_code: 'BEARING-01',
  item_article: 'ПД-01',
  item_name: 'Подшипник ведущего вала',
  unit: 'шт',
  quantity: 12,
  received_qty: 4,
  remaining_qty: 8,
  delivery_date: null,
  need_date: '2026-07-25',
  overdue_days: 0,
  line_status: 'to_order',
  fact_status: 'available',
  fact_source: 'ledger',
  row_generator: 'mrp_reservation',
  can_materialize: true,
  materialize_disabled_reason: null,
  supply_phase: 'no_goods',
  counts_in_mrp: true,
  price: 100,
  amount: 1200,
  run_id: 17,
  run_ids: [17, 18],
  requirement_ids: [101, 102],
  reservation_ids: [201, 202],
  planning_stock_pool: 'default',
  required_qty: 12,
  realized_qty: 4,
  open_order_covered_qty: 0,
  to_order_qty: 8,
  open_order_covered_pct: 0,
  to_order_pct: 66.666667,
  period_label: 'Июль 2026',
  horizon_bucket_count: 2,
}

async function mockPurchaseApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/purchase-control/orders') {
      await route.fulfill({
        json: {
          rows: [buyRow],
          total: 1,
          limit: 100,
          offset: 0,
          run_id: null,
          run_ids: [],
          truth_status: 'accepted',
          ledger_generation_id: 23,
          meta: {
            snapshot_id: 91,
            ledger_generation: 23,
            cutoff: '2026-07-23T12:00:00+00:00',
            truth_status: 'accepted',
            truth_reason: null,
            fact_source: 'ledger',
            received_qty_status: 'available',
            read_only: true,
          },
          summary: {
            total_rows: 1,
            by_status: { to_order: 1 },
            by_phase: { no_goods: 1, in_transit: 0, in_stock: 0 },
            to_order: 1,
            overdue: 0,
            expected_7d: 1,
            in_transit_amount: 0,
            fact_status: 'available',
          },
        },
      })
      return
    }
    if (url.pathname === '/api/v1/purchase-control/filters') {
      await route.fulfill({
        json: {
          suppliers: [{ supplier_id: 7, supplier_name: 'Промснаб' }],
          states: ['К поступлению'],
        },
      })
      return
    }
    if (url.pathname === '/api/v1/purchase-control/orders/8') {
      await route.fulfill({
        json: {
          order: {
            order_id: 8,
            order_number: 'ЗП-000008',
            order_date: '2026-07-20',
            order_ref1c: 'order-ref-8',
            order_state_name: 'К поступлению',
            supply_phase: 'in_transit',
            counts_in_mrp: true,
            deletion_mark: false,
            is_posted: true,
            document_amount: 1200,
            active: true,
            source: 'mrp',
            supplier_id: 7,
            supplier_name: 'Промснаб',
          },
          lines: [buyRow],
          meta: {
            snapshot_id: 91,
            ledger_generation: 23,
            cutoff: '2026-07-23T12:00:00+00:00',
            truth_status: 'accepted',
            truth_reason: null,
            fact_source: 'ledger',
            received_qty_status: 'unavailable',
            read_only: true,
          },
        },
      })
      return
    }
    await route.fulfill({ status: 500, json: { detail: `Unexpected visual-test request: ${url.pathname}` } })
  })
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('purchase control visual contract', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockPurchaseApi(page)

  await page.goto('/#/purchase-control')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Журнал закупок' })).toBeVisible()
  await expect(page.getByText('Ledger: 23')).toBeVisible()
  await expect(page.getByText('Под заказ (MRP)', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/в живых заказах 0 шт \(0%\) · к заказу 8 шт \(66,667%\)/)).toBeVisible()
  await expect(page.getByText('Ожидается за 7 дн: 1')).toBeVisible()

  await expect(page.locator('.app')).toHaveScreenshot('purchase-control.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
