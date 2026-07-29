import { expect, test, type Page } from '@playwright/test'

async function mockDbrSettingsApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname, search } = new URL(request.url())
    const key = `${request.method()} ${pathname}${search}`
    const fixtures: Record<string, unknown> = {
      'GET /api/v1/dbr/settings': {
        id: 1,
        frozen_days: 3,
        gate_horizon_workdays: 8,
        shelf_threshold_qty: '12.5',
        rt_machining_days: 2,
        rt_welding_days: 3,
        rt_painting_days: 4,
        batch_days_turning: 1,
        batch_days_bending: 2,
        batch_days_welding: 3,
        batch_days_paint_black: 4,
        batch_days_paint_color: 5,
        feeder_chain_enabled: true,
        feeder_load_horizon_weeks: 6,
        w2_warehouse_ref1c: 'Склад материалов',
        w3_warehouse_ref1c: 'Склад комплектации',
        w4_warehouse_ref1c: 'Склад готовой продукции',
        fastener_categories: ['Болты', 'Гайки', 'Шайбы'],
      },
      'GET /api/v1/dbr/assembly-rates': [
        {
          id: 11,
          resource_id: 4,
          resource_name: 'Сборочный участок',
          item_id: 77,
          item_code: 'РЕД-100.00',
          item_name: 'Редуктор приводной',
          qty_per_capacity: '8.5',
        },
        {
          id: 12,
          resource_id: 5,
          resource_name: 'Участок испытаний',
          item_id: 78,
          item_code: 'НАС-220.00',
          item_name: 'Насос промышленный',
          qty_per_capacity: '4',
        },
      ],
      'GET /api/v1/dbr/category-risks': [
        {
          id: 21,
          item_group: 'Подшипники',
          receipt_warehouse_ref1c: 'Склад материалов',
          supply_risk_pct: '15',
        },
        {
          id: 22,
          item_group: 'Электрика',
          receipt_warehouse_ref1c: 'Склад комплектации',
          supply_risk_pct: '8.5',
        },
      ],
      'GET /api/v1/resources/': [
        { resource_id: 4, resource_name: 'Сборочный участок' },
        { resource_id: 5, resource_name: 'Участок испытаний' },
      ],
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

test('DBR settings loaded visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-21T09:00:00Z'))
  await mockDbrSettingsApi(page)

  await page.goto('/#/dbr/settings')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Настройки DBR' })).toBeVisible()
  await expect(page.getByLabel('Порог полки, шт')).toHaveValue('12.5')
  await expect(page.getByLabel('Категории метизов (по одной в строке)')).toHaveValue('Болты\nГайки\nШайбы')
  await expect(page.getByText('ITEM-77 · ID 77')).toHaveCount(0)
  await expect(page.getByText('РЕД-100.00 · ID 77')).toBeAttached()
  await expect(page.locator('input[value="Подшипники"]')).toBeAttached()
  await expect(page.locator('.runBadge')).toHaveText('Тактов: 2 · Рисков: 2')
  await expect(page.locator('.statusBar')).toContainText('Строки 1-1 из 1')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('dbr-settings-loaded.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
