import { expect, test, type Page } from '@playwright/test'

async function mockDbrDrumBoardApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const key = `${request.method()} ${url.pathname}`

    if (key === 'GET /api/v1/dbr/drum/active/board') {
      expect(Object.fromEntries(url.searchParams)).toEqual({})
      await route.fulfill({
        json: {
          meta: { snapshot_id: 83, ledger_generation: 42, cutoff: '2026-07-21T09:00:00Z', runs: [{ run_id: 31, freeze_version: 9 }], read_only: true, unavailable_sections: ['kit_gate', 'execution'] },
          schedule: {
            id: 47,
            period_from: '2026-07-20',
            period_to: '2026-07-31',
            source_program_id: 31,
            status: 'active',
          },
          days: ['2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23', '2026-07-24'],
          resources: [
            { id: 11, name: 'Сборочный участок' },
            { id: 12, name: 'Участок испытаний' },
          ],
          slots: [
            {
              id: 101,
              date: '2026-07-20',
              resource_id: 11,
              resource_name: 'Сборочный участок',
              item_id: 501,
              item_code: 'РЕД-100.00',
              item_name: 'Редуктор приводной',
              qty: 10,
              produced_qty: null,
              kit_status: 'unknown', kit_gate_status: 'unavailable', execution_status: 'unavailable',
              release_status: 'pending',
              shortage: [],
              position: 1,
            },
            {
              id: 102,
              date: '2026-07-21',
              resource_id: 11,
              resource_name: 'Сборочный участок',
              item_id: 502,
              item_code: 'НАС-220.00',
              item_name: 'Насос промышленный',
              qty: 8,
              produced_qty: null,
              kit_status: 'unknown', kit_gate_status: 'unavailable', execution_status: 'unavailable',
              release_status: 'pending',
              shortage: [{ item: 'Электродвигатель', required: 8, available: 5, warehouse: 'Склад комплектации' }],
              position: 2,
            },
            {
              id: 103,
              date: '2026-07-22',
              resource_id: 11,
              resource_name: 'Сборочный участок',
              item_id: 503,
              item_code: 'СТ-410.00',
              item_name: 'Станция гидравлическая',
              qty: 6,
              produced_qty: null,
              kit_status: 'unknown', kit_gate_status: 'unavailable', execution_status: 'unavailable',
              release_status: 'pending',
              shortage: [{ item: 'Гидрораспределитель', required: 6, available: 1, warehouse: 'Основной склад' }],
              position: 3,
            },
            {
              id: 104,
              date: '2026-07-23',
              resource_id: 12,
              resource_name: 'Участок испытаний',
              item_id: 504,
              item_code: 'БЛ-150.00',
              item_name: 'Блок управления',
              qty: 12,
              produced_qty: null,
              kit_status: 'unknown', kit_gate_status: 'unavailable', execution_status: 'unavailable',
              release_status: 'released',
              one_c_order_number: 'ЗП-000047',
              shortage: [],
              position: 1,
            },
          ],
          gaps: [
            {
              id: 301,
              date: '2026-07-22',
              resource_id: 11,
              resource_name: 'Сборочный участок',
              item_id: 503,
              item_code: 'СТ-410.00',
              item_name: 'Станция гидравлическая',
              required_qty: 6,
              takt_qty: 4,
              gap_qty: 2,
            },
          ],
          kpi: { green: null, yellow: null, red: null, unknown: null, slots: 4, plan_qty: 36, fact_qty: null, kit_gate_status: 'unavailable', execution_status: 'unavailable' },
          calendar_fallback: true,
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

test('DBR active drum board visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-21T09:00:00Z'))
  await mockDbrDrumBoardApi(page)

  await page.goto('/#/dbr')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Барабан сборки' })).toBeVisible()
  await expect(page.locator('.runBadge')).toHaveText('График №47 · active')
  await expect(page.getByText(/Календарь работ не покрывает/)).toBeVisible()
  await expect(page.getByRole('button', { name: /—\/10.*Редуктор приводной/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /—\/6.*Станция гидравлическая/ })).toBeVisible()
  await expect(page.getByTestId('drum-snapshot-lineage')).toContainText('Ledger-поколение #42')
  await expect(page.getByRole('heading', { name: 'Разрывы мощности' })).toBeVisible()
  await expect(page.locator('.statusBar')).toContainText('Строки 1-4 из 4')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('dbr-drum-board-active.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
