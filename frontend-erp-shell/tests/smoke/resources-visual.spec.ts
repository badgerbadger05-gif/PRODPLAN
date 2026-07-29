import { expect, test, type Page } from '@playwright/test'

const resources = [
  {
    resource_id: 1,
    resource_name: 'Механический участок',
    capacity: 80,
    daily_work_hours: 8,
    work_schedule: '5/2',
    buffer_days: 2,
    shift_offset: 1,
    planning_range: 30,
  },
  {
    resource_id: 2,
    resource_name: 'Сборочный участок',
    capacity: 40,
    daily_work_hours: 12,
    work_schedule: '2/2',
    buffer_days: 1,
    shift_offset: 0,
    planning_range: 21,
  },
]

async function mockResourcesApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    const key = `${request.method()} ${pathname}`
    const fixtures: Record<string, unknown> = {
      'GET /api/v1/resources/': resources,
      'GET /api/v1/resources/production-kinds': [
        { id: 10, name: 'Мехобработка' },
        { id: 11, name: 'Покраска' },
        { id: 12, name: 'Сборка' },
      ],
      'GET /api/v1/resources/1/stages': [
        { id: 101, resource_id: 1, stage_id: 501, stage_name: 'Токарная обработка' },
        { id: 102, resource_id: 1, stage_id: 502, stage_name: 'Фрезерная обработка' },
      ],
      'GET /api/v1/resources/1/production-kinds': [
        {
          id: 201,
          resource_id: 1,
          production_kind_id: 10,
          production_kind_name: 'Мехобработка',
        },
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

test('resources editor visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockResourcesApi(page)

  await page.goto('/#/resources')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Производственные ресурсы' })).toBeVisible()
  await expect(page.getByRole('row', { name: /Механический участок/ })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByLabel('Название участка')).toHaveValue('Механический участок')
  await expect(page.getByRole('button', {
    name: 'Удалить вид производства Мехобработка',
  })).toBeVisible()
  await expect(page.getByText('Токарная обработка')).toBeVisible()
  await expect(page.getByText('Фрезерная обработка')).toBeVisible()
  await expect(page.locator('.statusBar')).toContainText('Строки 1-2 из 2')

  await expect(page.locator('.app')).toHaveScreenshot('resources-editor.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
