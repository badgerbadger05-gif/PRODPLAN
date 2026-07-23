import { expect, test, type Page } from '@playwright/test'

async function mockDbrPurchaseApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname, search } = new URL(request.url())
    const key = `${request.method()} ${pathname}${search}`
    const fixtures: Record<string, unknown> = {
      'GET /api/v1/dbr/purchase/cockpit': {
        meta: {
          snapshot_id: 71,
          ledger_generation: 42,
          cutoff: '2026-07-21T09:00:00Z',
          runs: [{ run_id: 31, freeze_version: 9 }],
          truth_status: 'accepted',
          read_only: true,
        },
        rows: [
          {
            item_id: 101,
            item_code: 'ПОД-6205',
            item_name: 'Подшипник радиальный 6205',
            supplier_ref1c: 'ООО Подшипник-Сервис',
            stock_qty: 12,
            exact_future_supply_qty: 8,
            outstanding_obligation_qty: 48,
            uncovered_qty: 28,
            to_order_qty: 28,
            need_date: '2026-08-18',
            warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [101],
            obligations: [{ reservation_id: 101, priority_period_from: '2026-08-18', priority_period_to: '2026-08-18', outstanding_qty: 48, uncovered_qty: 28, coverage: [] }],
          },
          {
            item_id: 102,
            item_code: 'ДВ-5.5',
            item_name: 'Электродвигатель 5,5 кВт',
            supplier_ref1c: null,
            stock_qty: 2,
            exact_future_supply_qty: 0,
            outstanding_obligation_qty: 10,
            uncovered_qty: 8,
            to_order_qty: 8,
            need_date: '2026-08-25',
            warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [102],
            obligations: [{ reservation_id: 102, priority_period_from: '2026-08-25', priority_period_to: '2026-08-25', outstanding_qty: 10, uncovered_qty: 8, coverage: [] }],
          },
          {
            item_id: 103,
            item_code: 'ЛИСТ-09Г2С-8',
            item_name: 'Лист 09Г2С, 8 мм',
            supplier_ref1c: 'АО Металлоснабжение',
            stock_qty: 400,
            exact_future_supply_qty: 500,
            outstanding_obligation_qty: 1250,
            uncovered_qty: 350,
            to_order_qty: 350,
            need_date: '2026-10-15',
            warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [103],
            obligations: [{ reservation_id: 103, priority_period_from: '2026-10-15', priority_period_to: '2026-10-15', outstanding_qty: 1250, uncovered_qty: 350, coverage: [] }],
          },
          {
            item_id: 104,
            item_code: 'КРЕП-М12',
            item_name: 'Комплект крепежа М12',
            supplier_ref1c: 'ООО Крепёж',
            stock_qty: 150,
            exact_future_supply_qty: 0,
            outstanding_obligation_qty: 100,
            uncovered_qty: 0,
            to_order_qty: 0,
            need_date: '2026-08-30',
            warehouse_ref1c: 'W4', planning_stock_pool: 'main', reservation_ids: [104],
            obligations: [{ reservation_id: 104, priority_period_from: '2026-08-30', priority_period_to: '2026-08-30', outstanding_qty: 100, uncovered_qty: 0, coverage: [] }],
          },
        ],
      },
    }

    if (key in fixtures) {
      await route.fulfill({ json: fixtures[key] })
      return
    }
    await route.fulfill({
      status: 500,
      json: { detail: `Unexpected visual-test request: ${key}` },
    })
  })
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('DBR purchase saved cockpit visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-21T09:00:00Z'))
  await mockDbrPurchaseApi(page)

  await page.goto('/#/dbr/purchase')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })
  await expect(page.getByRole('heading', { name: 'Закупка под план' })).toBeVisible()
  await expect(page.getByText('ПОД-6205')).toBeVisible()
  await expect(page.getByText('ДВ-5.5', { exact: true })).toBeVisible()
  await expect(page.getByText('ЛИСТ-09Г2С-8')).toBeVisible()
  await expect(page.getByText('КРЕП-М12')).toHaveCount(0)
  await expect(page.getByTestId('purchase-snapshot-lineage')).toContainText('Ledger-поколение #42')
  await expect(page.getByTestId('purchase-snapshot-lineage')).toContainText('run #31')
  await expect(page.locator('.dbrKpi').filter({ has: page.getByText('К заказу', { exact: true }) })).toContainText('3')
  await expect(page.locator('.statusBar')).toContainText('Строки 1-3 из 4')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('dbr-purchase-preview.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
