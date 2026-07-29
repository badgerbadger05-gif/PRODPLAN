import { expect, test } from '@playwright/test'

const distribution = {
  asOf: '2026-07-20T12:00:00',
  resources: [
    {
      resource_id: 1,
      resource_name: 'Механический участок',
      norm_hours: 7,
      products: [
        {
          root_item_id: 100,
          root_item_code: 'PUMP-01',
          root_item_name: 'Насос',
          components: [{
            item_id: 501,
            item_code: 'SHAFT-01',
            item_article: 'ВАЛ-01',
            item_name: 'Вал ведущий',
            qty_per_unit: 2,
            stock_qty: 6,
            norm_hours: 0.5,
            norm_hours_total: 2,
            stage_id: 8,
            stage_name: 'Токарная обработка',
          }],
        },
        {
          root_item_id: 101,
          root_item_code: 'GEAR-01',
          root_item_name: 'Редуктор',
          components: [{
            item_id: 501,
            item_code: 'SHAFT-01',
            item_article: 'ВАЛ-01',
            item_name: 'Вал ведущий',
            qty_per_unit: 3,
            stock_qty: 6,
            norm_hours: 0.5,
            norm_hours_total: 3,
            stage_id: 8,
            stage_name: 'Токарная обработка',
          }],
        },
      ],
    },
    {
      resource_id: 2,
      resource_name: 'Сборочный участок',
      norm_hours: 1.5,
      products: [{
        root_item_id: 100,
        root_item_code: 'PUMP-01',
        root_item_name: 'Насос',
        components: [{
          item_id: 601,
          item_code: 'BODY-01',
          item_article: 'КОРП-01',
          item_name: 'Корпус насоса',
          qty_per_unit: 1,
          stock_qty: 2,
          norm_hours: 1.5,
          norm_hours_total: 1.5,
          stage_id: 10,
          stage_name: 'Сборка',
        }],
      }],
    },
  ],
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('stage distribution visual contract', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (
      route.request().method() === 'POST'
      && url.pathname === '/api/v1/resources/calculate_distribution'
    ) {
      await route.fulfill({ json: distribution })
      return
    }
    await route.fulfill({
      status: 500,
      json: { detail: `Unexpected visual-test request: ${route.request().method()} ${url.pathname}` },
    })
  })

  await page.goto('/#/stage-distribution')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Распределение этапов' })).toBeVisible()
  await page.getByRole('button', { name: 'Рассчитать' }).click()
  await expect(page.getByText('Распределение рассчитано')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Механический участок · 7 н/ч' })).toHaveClass('activeTab')
  await expect(page.getByText('Вал ведущий')).toBeVisible()
  await expect(page.locator('.statusBar')).toContainText('Строки 1-1 из 1')

  await expect(page.locator('.app')).toHaveScreenshot('stage-distribution.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
