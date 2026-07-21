import { expect, test, type Page } from '@playwright/test'

async function mockDbrPurchaseApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname, search } = new URL(request.url())
    const key = `${request.method()} ${pathname}${search}`
    const fixtures: Record<string, unknown> = {
      'GET /api/v1/dbr/programs': [
        {
          id: 31,
          title: 'Программа выпуска · август 2026',
          company: 'ООО ЗСМ',
          from_date: '2026-08-01',
          to_date: '2026-08-31',
          status: 'approved',
          items: [],
        },
      ],
      'GET /api/v1/dbr/purchase-plan/preview?active=true&threshold_days=60': {
        ok: true,
        source: { kind: 'active' },
        lead_time_threshold_days: 60,
        rows_to_order: 3,
        items_total: 4,
        warnings: ['Для двигателя ДВ-5.5 не назначен поставщик'],
        rows: [
          {
            item_id: 101,
            item_code: 'ПОД-6205',
            item_name: 'Подшипник радиальный 6205',
            supplier_ref1c: 'ООО Подшипник-Сервис',
            demand_qty: 48,
            stock_qty: 12,
            open_order_qty: 8,
            available_qty: 20,
            to_order_qty: 28,
            need_date: '2026-08-18',
            replenishment_time: 21,
            order_before: '2026-07-28',
            within_lead_time_threshold: true,
          },
          {
            item_id: 102,
            item_code: 'ДВ-5.5',
            item_name: 'Электродвигатель 5,5 кВт',
            supplier_ref1c: null,
            demand_qty: 10,
            stock_qty: 2,
            open_order_qty: 0,
            available_qty: 2,
            to_order_qty: 8,
            need_date: '2026-08-25',
            replenishment_time: 35,
            order_before: '2026-07-21',
            within_lead_time_threshold: true,
          },
          {
            item_id: 103,
            item_code: 'ЛИСТ-09Г2С-8',
            item_name: 'Лист 09Г2С, 8 мм',
            supplier_ref1c: 'АО Металлоснабжение',
            demand_qty: 1250,
            stock_qty: 400,
            open_order_qty: 500,
            available_qty: 900,
            to_order_qty: 350,
            need_date: '2026-10-15',
            replenishment_time: 18,
            order_before: '2026-09-27',
            within_lead_time_threshold: false,
          },
          {
            item_id: 104,
            item_code: 'КРЕП-М12',
            item_name: 'Комплект крепежа М12',
            supplier_ref1c: 'ООО Крепёж',
            demand_qty: 100,
            stock_qty: 150,
            open_order_qty: 0,
            available_qty: 150,
            to_order_qty: 0,
            need_date: '2026-08-30',
            replenishment_time: 5,
            order_before: null,
            within_lead_time_threshold: false,
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

test('DBR purchase loaded preview visual baseline', async ({ page }) => {
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
  await page.getByRole('button', { name: 'Рассчитать' }).click()

  await expect(page.getByRole('heading', { name: 'Закупка под план' })).toBeVisible()
  await expect(page.getByText('ПОД-6205')).toBeVisible()
  await expect(page.getByText('ДВ-5.5', { exact: true })).toBeVisible()
  await expect(page.getByText('ЛИСТ-09Г2С-8')).toBeVisible()
  await expect(page.getByText('КРЕП-М12')).toHaveCount(0)
  await expect(page.getByText('Предупреждения качества: 1')).toBeVisible()
  await expect(page.locator('.dbrKpi').filter({ has: page.getByText('К заказу', { exact: true }) })).toContainText('3')
  await expect(page.locator('.statusBar')).toContainText('Строки 1-3 из 4')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('dbr-purchase-preview.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
