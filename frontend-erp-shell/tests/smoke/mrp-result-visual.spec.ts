import { expect, test, type Page } from '@playwright/test'

async function mockMrpResultApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const key = `${request.method()} ${url.pathname}`

    if (key === 'GET /api/v1/plan/results/77') {
      await route.fulfill({
        json: {
          run: {
            run_id: 77,
            status: 'SUCCESS',
            started_at: '2026-07-20T08:15:00Z',
            finished_at: '2026-07-20T08:18:42Z',
            horizon_days: 30,
            source_plan_id: 12,
            source_plan_name: 'Основной план · июль 2026',
          },
          counts: {
            production_orders: 4,
            purchase_requests: 3,
            rework_requests: 1,
          },
          capacity: {
            overloaded_buckets: 2,
            overload_total: 18.5,
            hours_planned_total: 264,
            hours_available_total: 245.5,
          },
        },
      })
      return
    }

    if (key === 'GET /api/v1/plan/results/77/production') {
      expect(url.searchParams.get('limit')).toBe('200')
      expect(url.searchParams.get('offset')).toBe('0')
      await route.fulfill({
        json: {
          rows: [
            {
              order_id: 701,
              item_id: 101,
              item_name: 'Корпус редуктора',
              item_article: 'КР-100.01',
              unit: 'шт',
              qty: 24,
              need_date: '2026-07-24',
              start_date: '2026-07-20',
              finish_date: '2026-07-23',
              forecast_date: '2026-07-23',
              forecast_shift_days: 0,
              main_area_name: 'Механический участок',
              main_stage_name: 'Мехобработка',
              norm_hours_total: 72,
              badge: 'Критический путь',
            },
            {
              order_id: 702,
              item_id: 102,
              item_name: 'Вал приводной',
              item_article: 'ВП-220.04',
              unit: 'шт',
              qty: 18,
              need_date: '2026-07-25',
              start_date: '2026-07-21',
              finish_date: '2026-07-24',
              forecast_date: '2026-07-27',
              forecast_shift_days: 3,
              forecast_reason: 'Ожидание поковки',
              main_area_name: 'Токарный участок',
              norm_hours_total: 45,
            },
            {
              order_id: 703,
              source_order_ids: [703, 704],
              item_id: 103,
              item_name: 'Узел подшипниковый',
              item_article: 'УП-310.00',
              unit: 'шт',
              qty: 12,
              need_date: '2026-07-26',
              start_date: '2026-07-22',
              finish_date: '2026-07-25',
              forecast_date: '2026-08-01',
              forecast_shift_days: 7,
              forecast_reason: 'Дефицит подшипников',
              main_area_name: 'Сборочный участок',
              norm_hours_total: 31.5,
              badge: 'Объединено: 2 заказа',
            },
            {
              order_id: 705,
              item_id: 104,
              item_name: 'Крышка защитная',
              item_article: 'КЗ-115.02',
              unit: 'шт',
              qty: 0,
              need_date: '2026-07-28',
              start_date: '2026-07-24',
              finish_date: '2026-07-27',
              main_area_name: 'Листовой участок',
              norm_hours_total: 0,
              badge: 'Потребность закрыта',
            },
          ],
          total: 4,
          total_qty: 54,
          limit: 200,
          offset: 0,
        },
      })
      return
    }

    if (key === 'GET /api/v1/plan/period-plans/12/matrix') {
      await route.fulfill({
        json: {
          rows: [
            { item_id: 101, item_name: 'Корпус редуктора', item_article: 'КР-100.01', item_code: 'KR-100' },
            { item_id: 102, item_name: 'Вал приводной', item_article: 'ВП-220.04', item_code: 'VP-220' },
          ],
          buckets: [],
        },
      })
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

test('MRP result production cockpit visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockMrpResultApi(page)

  await page.goto('/#/mrp-runs/77')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Результаты MRP #77' })).toBeVisible()
  await expect(page.locator('.runBadge')).toHaveText('Успешно')
  await expect(page.getByRole('row', { name: /Корпус редуктора/ })).toBeVisible()
  await expect(page.getByRole('row', { name: /Узел подшипниковый/ })).toContainText('+7 дн · 01.08')
  await expect(page.getByRole('button', { name: 'Создать заказы (0)' })).toBeDisabled()
  await expect(page.locator('.statusBar')).toContainText('Строки 1-4 из 4')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('mrp-result-production.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
