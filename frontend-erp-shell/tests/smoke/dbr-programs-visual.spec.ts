import { expect, test, type Page } from '@playwright/test'

async function mockDbrProgramsApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname, search } = new URL(request.url())
    const key = `${request.method()} ${pathname}${search}`

    if (key === 'GET /api/v1/dbr/programs') {
      await route.fulfill({
        json: [
          {
            id: 31,
            source_run_id: 71,
            ledger_generation_id: 14,
            freeze_version: 2,
            title: 'Программа выпуска · август 2026',
            company: 'ООО ЗСМ',
            from_date: '2026-08-01',
            to_date: '2026-08-31',
            status: 'draft',
            items: [
              {
                id: 311,
                item_id: 101,
                item_code: 'РЕД-100.00',
                item_name: 'Редуктор приводной',
                program_date: '2026-08-05',
                qty: 24,
                comment: 'Первая партия',
              },
              {
                id: 312,
                item_id: 102,
                item_code: 'НАС-220.00',
                item_name: 'Насос промышленный',
                program_date: '2026-08-12',
                qty: 16,
                comment: null,
              },
            ],
          },
          {
            id: 30,
            source_run_id: 71,
            ledger_generation_id: 14,
            freeze_version: 2,
            title: 'Контрактный план · июль 2026',
            company: 'ООО ЗСМ',
            from_date: '2026-07-01',
            to_date: '2026-07-31',
            status: 'approved',
            items: [
              {
                id: 301,
                item_id: 103,
                item_code: 'СТ-410.00',
                item_name: 'Станция гидравлическая',
                program_date: '2026-07-24',
                qty: 8,
                comment: 'Утверждено производством',
              },
            ],
          },
          {
            id: 29,
            source_run_id: 71,
            ledger_generation_id: 14,
            freeze_version: 2,
            title: 'Резерв мощностей · июль',
            company: null,
            from_date: '2026-07-15',
            to_date: '2026-07-31',
            status: 'approved',
            items: [],
          },
        ],
      })
      return
    }

    if (key === 'GET /api/v1/plan/runs?limit=200') {
      await route.fulfill({
        json: {
          rows: [
            {
              run_id: 71,
              status: 'FIXED_SNAPSHOT',
              started_at: '2026-07-20T09:00:00Z',
              finished_at: '2026-07-20T09:03:00Z',
              source_plan_id: 8,
              source_plan_name: 'Август 2026',
            },
          ],
          total: 1,
          limit: 200,
          offset: 0,
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

test('DBR programs loaded list visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-21T09:00:00Z'))
  await mockDbrProgramsApi(page)

  await page.goto('/#/dbr/programs')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Производственные программы' })).toBeVisible()
  await expect(page.getByLabel('Период с')).toHaveValue('2026-07-21')
  await expect(page.locator('.runBadge')).toHaveText('Программ: 3')
  await expect(page.getByRole('row', { name: /Программа выпуска · август 2026/ })).toContainText('Черновик')
  await expect(page.getByRole('row', { name: /Контрактный план · июль 2026/ })).toContainText('Утверждена')
  await expect(page.getByRole('row', { name: /Резерв мощностей · июль/ })).toBeAttached()
  await expect(page.locator('.statusBar')).toContainText('Строки 1-3 из 3')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('dbr-programs-list.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
