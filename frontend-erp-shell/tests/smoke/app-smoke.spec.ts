import { expect, test, type Page } from '@playwright/test'

const planningRun = {
  run_id: 42,
  status: 'completed',
  started_at: '2026-07-20T10:00:00',
  finished_at: '2026-07-20T10:05:00',
  period_from: '2026-07-21',
  period_to: '2026-07-31',
  source_plan_id: 7,
  source_plan_name: 'Июль',
  requirement_count: 120,
  requirement_remaining_qty: 15,
  order_count: 18,
  purchase_count: 6,
  overload_buckets: 2,
}

const transfer = {
  issue_id: 5,
  document_number: 'ПМ-000005',
  status: 'exported',
  product_id: 11,
  order_id: 12,
  order_number: 'ЗСНФ-001',
  order_prodplan_number: 'ПП-001',
  order_one_c_number: 'ЗСНФ-001',
  order_ref1c: 'order-ref',
  item_name: 'Корпус редуктора',
  item_article: 'КР-01',
  quantity: 10,
  remaining_qty: 4,
  unit: 'шт',
  source_warehouse_ref1c: 'wh-source',
  source_warehouse_name: 'Заготовительный участок',
  warehouse_ref1c: 'wh-destination',
  destination_warehouse_name: 'Сборочный участок',
  exported_ref1c: 'transfer-ref',
  one_c_number: 'ПТ-000005',
  can_assemble: true,
  line_status: 'to_move',
  lines_count: 1,
}

async function mockApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/plan/runs') {
      await route.fulfill({ json: { rows: [planningRun], total: 1, limit: 30, offset: 0 } })
      return
    }
    if (url.pathname === '/api/v1/resources/') {
      await route.fulfill({ json: [] })
      return
    }
    if (url.pathname.includes('/warehouses')) {
      await route.fulfill({ json: { rows: [], total: 0, selected_total: 0 } })
      return
    }
    if (url.pathname === '/api/v1/production-control/material-issues') {
      await route.fulfill({
        json: {
          rows: [transfer],
          total: 1,
          limit: 100,
          offset: 0,
          source_warehouses: [{
            warehouse_ref1c: 'wh-source',
            warehouse_name: 'Заготовительный участок',
          }],
        },
      })
      return
    }
    if (url.pathname === '/api/v1/production-control/material-issues/5') {
      await route.fulfill({
        json: {
          ...transfer,
          lines: [{
            line_id: 9,
            component_item_id: 15,
            item_name: 'Втулка',
            item_article: 'ВТ-15',
            required_qty: 4,
            issued_qty: 3,
            line_status: 'planned',
          }],
        },
      })
      return
    }
    await route.fulfill({ status: 200, json: { rows: [], total: 0 } })
  })
}

test.beforeEach(async ({ page }) => {
  await mockApi(page)
})

test('opens the lazy ERP shell without a backend', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => consoleErrors.push(error.message))

  await page.goto('/')
  await expect(page.locator('.brand').getByText('PRODPLAN', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Главная' })).toBeVisible()
  await expect(page.getByText('#42')).toBeVisible()

  await page.getByRole('link', { name: 'MRP прогоны' }).click()
  await expect(page.getByRole('heading', { name: 'MRP планирование' })).toBeVisible()
  await expect(page.getByText('Прогон #42')).toBeVisible()

  await page.getByRole('link', { name: 'Заявки перемещений' }).click()
  await expect(page.getByRole('heading', { name: 'Заявки на перемещение' })).toBeVisible()
  await expect(page.getByText('ПМ-000005')).toBeVisible()
  await expect(page.getByText('Втулка')).toBeVisible()

  expect(consoleErrors).toEqual([])
})
