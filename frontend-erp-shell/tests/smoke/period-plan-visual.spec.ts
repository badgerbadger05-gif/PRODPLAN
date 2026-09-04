import { expect, test, type Page } from '@playwright/test'

const draftPlan = {
  id: 123,
  name: 'МАЙ 2026',
  status: 'fixed',
  period_from: '2026-05-01',
  period_to: '2026-05-29',
  comment: 'Основной производственный план',
  created_by: 'Иван',
  created_at: '2026-04-01T10:00:00',
  fixed_at: '2026-05-01T08:00:00+03:00',
  fixed_by: 'Иван',
  line_count: 1,
  planned_output_qty: 24,
  accepted_plan_output_qty: 6,
  assembly_remaining_qty: 18,
  plan_output_truth_status: 'accepted',
  plan_output_truth_reason: null,
  plan_output_generation_id: 77,
  plan_output_cutoff: '2026-07-20T12:00:00Z',
}

const closedPlan = {
  ...draftPlan,
  id: 122,
  name: 'АПРЕЛЬ 2026',
  status: 'closed',
  period_from: '2026-04-03',
  period_to: '2026-04-24',
  comment: 'Закрытый план',
  created_at: '2026-03-01T09:00:00',
  line_count: 4,
}

async function mockPeriodPlanApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/plan/period-plans') {
      await route.fulfill({ json: { rows: [draftPlan, closedPlan], total: 2 } })
      return
    }
    if (url.pathname === '/api/v1/plan/period-plans/123/matrix') {
      await route.fulfill({
        json: {
          plan: draftPlan,
          planned_output_qty: 24,
          accepted_plan_output_qty: 6,
          assembly_remaining_qty: 18,
          plan_output_truth_status: 'accepted',
          plan_output_truth_reason: null,
          plan_output_generation_id: 77,
          plan_output_cutoff: '2026-07-20T12:00:00Z',
          buckets: ['2026-05-01', '2026-05-08', '2026-05-15', '2026-05-22'],
          bucket_totals: {
            '2026-05-01': 4,
            '2026-05-08': 6,
            '2026-05-15': 8,
            '2026-05-22': 6,
          },
          rows: [{
            item_id: 501,
            item_code: 'C-501',
            item_name: 'Насос ГА-1',
            item_article: 'ART-501',
            total_qty: 24,
            planned_output_qty: 24,
            accepted_plan_output_qty: 6,
            assembly_remaining_qty: 18,
            output_by_bucket: {
              '2026-05-01': { planned_output_qty: 4, accepted_plan_output_qty: 4, assembly_remaining_qty: 0 },
              '2026-05-08': { planned_output_qty: 6, accepted_plan_output_qty: 2, assembly_remaining_qty: 4 },
              '2026-05-15': { planned_output_qty: 8, accepted_plan_output_qty: 0, assembly_remaining_qty: 8 },
              '2026-05-22': { planned_output_qty: 6, accepted_plan_output_qty: 0, assembly_remaining_qty: 6 },
            },
            buckets: {
              '2026-05-01': 4,
              '2026-05-08': 6,
              '2026-05-15': 8,
              '2026-05-22': 6,
            },
            locked_buckets: {},
          }],
          total_qty: 24,
          grand_total: 24,
          total: 24,
        },
      })
      return
    }
    if (url.pathname === '/api/v1/plan/period-plans/123/runs') {
      await route.fulfill({ json: { rows: [], total: 0 } })
      return
    }
    await route.fulfill({ status: 500, json: { detail: `Unexpected visual-test request: ${url.pathname}` } })
  })
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockPeriodPlanApi(page)
})

async function stabilize(page: Page) {
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })
}

test('period plan list visual contract', async ({ page }) => {
  await page.goto('/#/period-plan')
  await stabilize(page)

  await expect(page.getByRole('heading', { name: 'Планирование выпуска' })).toBeVisible()
  await expect(page.getByText('МАЙ 2026', { exact: true })).toBeVisible()
  await expect(page.getByText('АПРЕЛЬ 2026', { exact: true })).toBeVisible()
  await expect(page.getByRole('row', { name: /МАЙ 2026/ }).getByLabel('Факт выпуска плана')).toContainText('Принято Ledger 6')
  await expect(page.locator('.statusBar')).toContainText('Строки 1-2 из 2')

  await expect(page.locator('.app')).toHaveScreenshot('period-plan-list.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})

test('period plan detail visual contract', async ({ page }) => {
  await page.goto('/#/period-plan/123')
  await stabilize(page)

  await expect(page.getByRole('heading', { name: 'МАЙ 2026' })).toBeVisible()
  await expect(page.getByText('Насос ГА-1', { exact: true })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Итого выпуска' })).toBeVisible()
  await expect(page.locator('.periodPlanOutputFacts').getByLabel('Факт выпуска плана')).toContainText('Исходный план 24')
  await expect(page.locator('.periodPlanOutputFacts').getByLabel('Факт выпуска плана')).toContainText('Принято Ledger 6')
  await expect(page.locator('.periodPlanOutputFacts').getByLabel('Факт выпуска плана')).toContainText('Осталось выпустить 18')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('period-plan-detail.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
