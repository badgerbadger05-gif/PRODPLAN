import { expect, test } from '@playwright/test'

const sections = [
  { nav: 'Планирование выпуска', title: 'Планирование выпуска' },
  { nav: 'Очередь сборки', title: 'Очередь сборки' },
  { nav: 'Барабан', title: 'Барабан' },
  { nav: 'Полки', title: 'Полки' },
  { nav: 'Журнал заказов', title: 'Журнал заказов на производство' },
  { nav: 'Выпуск недельный', title: 'Отчёт о выпуске техники недельный' },
  { nav: 'MRP прогоны', title: 'MRP планирование' },
  { nav: 'Ресурсы', title: 'Производственные ресурсы' },
  { nav: 'Распределение этапов', title: 'Распределение этапов' },
  { nav: 'Спецификации', title: 'BOM cockpit' },
  { nav: 'Синхронизация', title: 'Синхронизация 1С OData' },
]

test('opens the ERP shell and critical sections', async ({ page, request }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (
      message.type() === 'error'
      && !message.text().startsWith('Failed to load resource:')
    ) {
      consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => {
    consoleErrors.push(error.message)
  })

  const health = await request.get('http://127.0.0.1:8000/health')
  expect(health.ok(), 'Backend must be running on http://127.0.0.1:8000').toBeTruthy()

  await page.goto('/')
  await expect(page.locator('.brand').getByText('PRODPLAN', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Главная' })).toBeVisible()

  for (const section of sections) {
    await page.getByRole('link', { name: section.nav, exact: true }).click()
    await expect(page.getByRole('heading', { name: section.title, exact: true })).toBeVisible()
  }

  expect(consoleErrors).toEqual([])
})
