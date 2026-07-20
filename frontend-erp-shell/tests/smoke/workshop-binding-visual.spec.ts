import { expect, test, type Page } from '@playwright/test'

const reviewItem = {
  item_id: 41,
  item_code: 'BEARING-01',
  item_name: 'Подшипник ведущего вала',
  item_article: 'ПД-01',
  active_lines: 1,
  reason_code: 'KIND_NOT_BOUND',
  reason_text: 'Вид производства не привязан к участку.',
  recommendation: 'Проверьте предложенный участок и подтвердите привязку.',
  spec_id: 3,
  spec_name: 'СП-03',
  production_kind_id: 5,
  production_kind_name: 'Мехобработка',
  suggested_resource_id: 1,
  suggested_resource_name: 'Механический участок',
  suggested_stage_id: 8,
  suggested_stage_name: 'Токарная обработка',
}

const orderLine = {
  product_id: 900,
  order_id: 700,
  order_number: 'ЗСНФ-000700',
  quantity: 10,
  remaining_qty: 4,
  status: 'ready',
}

async function mockWorkshopBindingApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/workshop-binding-review/items') {
      await route.fulfill({
        json: {
          items: [reviewItem],
          total: 1,
          limit: 100,
          offset: 0,
          scope: 'active',
          counts_by_reason: {
            NO_SPEC: 2,
            NO_PRODUCTION_KIND: 3,
            KIND_NOT_BOUND: 1,
            NO_WAREHOUSE_BINDING: 4,
          },
        },
      })
      return
    }
    if (pathname === '/api/v1/workshop-binding-review/items/41/lines') {
      await route.fulfill({
        json: {
          item_id: reviewItem.item_id,
          rows: [orderLine],
          total: 1,
        },
      })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({
        json: [
          { resource_id: 1, resource_name: 'Механический участок' },
          { resource_id: 2, resource_name: 'Сборочный участок' },
        ],
      })
      return
    }
    await route.fulfill({
      status: 500,
      json: { detail: `Unexpected visual-test request: ${pathname}` },
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

test('workshop binding review visual fixture', async ({ page }, testInfo) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockWorkshopBindingApi(page)

  await page.goto('/#/workshop-binding-review')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Разбор привязок' })).toBeVisible()
  await expect(page.getByText('Подшипник ведущего вала').first()).toBeVisible()
  await expect(page.getByText('Вид производства не привязан к участку.')).toBeVisible()
  await expect(page.getByText(/ЗСНФ-000700 · 4 шт/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Вид не привязан к участку (1)' })).toBeVisible()
  await expect(page.locator('.statusBar')).toContainText('Строки 1-1 из 1')

  // Keep a deterministic candidate artifact, but intentionally do not create a
  // repository baseline until the product migration has landed.
  await testInfo.attach('workshop-binding-review-candidate', {
    body: await page.locator('.app').screenshot({
      animations: 'disabled',
      caret: 'hide',
      scale: 'css',
    }),
    contentType: 'image/png',
  })
})
