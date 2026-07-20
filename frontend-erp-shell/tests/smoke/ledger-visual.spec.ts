import { expect, test } from '@playwright/test'

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('ledger workspace visual contract', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await page.route('**/api/**', (route) => route.abort('failed'))

  await page.goto('/#/ledger')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Производственный ledger' })).toBeVisible()
  await expect(page.getByText('P-1042', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Аудит' })).toBeVisible()
  await expect(page.locator('.ledgerStatus')).toContainText('Проводок: 3')

  await expect(page.locator('.app')).toHaveScreenshot('ledger-workspace.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
