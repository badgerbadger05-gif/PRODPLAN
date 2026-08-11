import { expect, test } from '@playwright/test'

const orders = [
  {
    product_id: 101,
    item_id: 201,
    order_number: 'ORD-1',
    order_prodplan_number: 'ПП-000101',
    order_source: 'mrp',
    source: 'mrp',
    order_ref1c: null,
    order_date: '2026-07-18',
    line_number: 1,
    item_name: 'Кронштейн опорный',
    item_article: 'КР-01',
    unit: 'шт',
    quantity: 10,
    produced_qty: 0,
    remaining_qty: 10,
    planned_start_date: '2026-07-21',
    planned_finish_date: '2026-07-23',
    forecast_date: '2026-07-24',
    forecast_shift_days: 1,
    forecast_status: 'delayed',
    forecast_reason: 'Ожидание комплектующих',
    status: 'ready',
    coverage_status: 'assembled',
    coverage_label: 'Собрано',
    issue_status: 'posted',
    issue_count: 1,
    workshop_name: 'Сборочный участок',
    stage_name: 'Сборка',
    optimal_batch: 5,
    source_plan_id: 7,
    source_plan_name: 'План июля',
  },
  {
    product_id: 102,
    item_id: 202,
    order_number: 'ЗСНФ-000202',
    order_prodplan_number: 'ПП-000102',
    order_source: '1c',
    source: '1c',
    order_ref1c: 'order-ref-102',
    order_one_c_number: 'ЗСНФ-000202',
    item_name: 'Вал приводной',
    item_article: 'ВП-02',
    unit: 'шт',
    quantity: 5,
    produced_qty: 0,
    remaining_qty: 5,
    planned_start_date: '2026-07-22',
    planned_finish_date: '2026-07-25',
    forecast_date: '2026-08-02',
    forecast_shift_days: 8,
    forecast_status: 'critical',
    forecast_reason: 'Дефицит подшипников',
    status: 'shortage',
    coverage_status: 'shortage',
    coverage_label: 'Дефицит',
    workshop_name: 'Механический участок',
    stage_name: 'Мехобработка',
  },
]

const materials = {
  order_number: 'ORD-1',
  item_name: 'Кронштейн опорный',
  coverage_status: 'assembled',
  coverage_label: 'Собрано',
  components: [{
    component_item_id: 301,
    item_name: 'Болт М8',
    item_article: 'BOLT-8',
    qty_per_unit: 4,
    available_qty: 40,
    required_qty: 40,
    missing_qty: 0,
    unit: 'шт',
    availability_status: 'assembled',
    coverage_status: 'assembled',
    coverage_label: 'Собрано',
  }],
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('production control visual contract', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/production-control/orders') {
      await route.fulfill({
        json: {
          rows: orders,
          total: orders.length,
          limit: 100,
          offset: 0,
          latest_run_id: 77,
          truth_meta: {
            ledger_generation: 77,
            cutoff: '2026-07-31T00:00:00Z',
            truth_status: 'accepted',
            truth_reason: null,
          },
        },
      })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({
        json: [
          { resource_id: 1, resource_name: 'Сборочный участок' },
          { resource_id: 2, resource_name: 'Механический участок' },
        ],
      })
      return
    }
    if (pathname === '/api/v1/plan/period-plans') {
      await route.fulfill({ json: { rows: [], total: 0, limit: 500, offset: 0 } })
      return
    }
    if (pathname === '/api/v1/production-control/orders/101/materials') {
      await route.fulfill({ json: materials })
      return
    }
    await route.abort('failed')
  })

  await page.goto('/#/production-control')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Журнал заказов на производство' })).toBeVisible()
  await expect(page.getByText('Кронштейн опорный').first()).toBeVisible()
  await expect(page.getByText('Вал приводной')).toBeVisible()
  await expect(page.getByText('Болт М8')).toBeVisible()
  await expect(page.locator('.runBadge')).toHaveText('MRP run: 77')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('production-control.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
