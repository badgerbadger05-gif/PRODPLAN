import { expect, test, type Page } from '@playwright/test'

const config = {
  base_url: 'https://erp.example.test/odata/standard.odata',
  username: 'visual.operator',
  password: '',
  token: '',
}

const warehouses = [
  {
    warehouse_id: 1,
    warehouse_ref1c: 'warehouse-main',
    warehouse_code: 'ОСН',
    warehouse_name: 'Основной склад',
    is_selected: true,
  },
  {
    warehouse_id: 2,
    warehouse_ref1c: 'warehouse-production',
    warehouse_code: 'ПРЗ',
    warehouse_name: 'Производственный склад',
    is_selected: false,
  },
]

const groups = [
  { id: 'group-pumps', code: 'НАС', name: 'Насосное оборудование' },
  { id: 'group-components', code: 'КОМ', name: 'Комплектующие' },
]

const responseSecret = 'MUST-NOT-RENDER-IN-VISUAL'

async function mockSyncApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    const key = `${request.method()} ${pathname}`

    if (key === 'GET /api/v1/odata/config') {
      await route.fulfill({ json: config })
      return
    }
    if (key === 'GET /api/v1/sync/warehouses') {
      await route.fulfill({
        json: { rows: warehouses, total: warehouses.length, selected_total: 1 },
      })
      return
    }
    if (key === 'GET /api/v1/odata/groups') {
      await route.fulfill({
        json: { items: groups, selected_ids: ['group-pumps'] },
      })
      return
    }
    if (key === 'POST /api/v1/odata/test') {
      expect(await request.postDataJSON()).toEqual(config)
      await route.fulfill({
        json: {
          status: 'ok',
          database: 'Управление производством',
          password: responseSecret,
          token: responseSecret,
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

test('sync hydrated and diagnostic success visual baselines', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockSyncApi(page)

  await page.goto('/#/sync')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Синхронизация 1С OData' })).toBeVisible()
  await expect(page.getByLabel('Базовый URL')).toHaveValue(config.base_url)
  await expect(page.getByLabel('Пользователь')).toHaveValue(config.username)
  await expect(page.getByText('Всего: 2 · Выбрано: 1')).toHaveCount(2)
  await expect(page.getByText('Операций пока не было')).toBeVisible()
  await expect(page.locator('.statusBar')).toContainText('Строки 0-0 из 0')
  await expect(page.locator('.app')).not.toContainText(responseSecret)

  await expect(page.locator('.app')).toHaveScreenshot('sync-hydrated.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })

  await page.getByRole('button', { name: 'Тест подключения' }).click()
  await expect(page.getByRole('status')).toContainText('Тест подключения: выполнено')
  await expect(page.locator('.syncLog')).toContainText('Тест подключения')
  await expect(page.locator('.syncLog')).toContainText('"password":"[REDACTED]"')
  await expect(page.locator('.syncLog')).toContainText('"token":"[REDACTED]"')
  await expect(page.locator('.syncLog')).not.toContainText(responseSecret)
  await expect(page.locator('.statusBar')).toContainText('Строки 1-2 из 2')

  await expect(page.locator('.app')).toHaveScreenshot('sync-diagnostic-success.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
