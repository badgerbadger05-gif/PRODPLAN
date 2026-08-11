import { expect, test, type Page } from '@playwright/test'

const pump = {
  item_id: 100,
  item_code: 'PUMP-01',
  item_name: 'Насос ГА-1',
  item_article: 'НАС-01',
  unit: 'шт',
  replenishment_method: 'Производство',
  spec_id: 10,
  spec_name: 'СП-10',
  has_children: true,
}

async function mockSpecificationApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    const key = `${request.method()} ${pathname}`

    if (key === 'GET /api/v1/specification/search') {
      await route.fulfill({ json: { items: [pump], meta: { q: 'НАС-01', count: 1, limit: 50 } } })
      return
    }
    if (key === 'GET /api/v1/specification/full') {
      await route.fulfill({
        json: {
          nodes: [{
            id: 'item-100',
            parentId: null,
            type: 'item',
            specId: 10,
            name: 'Насос ГА-1',
            article: 'НАС-01',
            unit: 'шт',
            replenishmentMethod: 'Производство',
            computed: { treeQty: 1 },
            children: [
              {
                id: 'item-200',
                parentId: 'item-100',
                type: 'item',
                componentId: 301,
                name: 'Подшипник ведущего вала',
                article: 'ПД-01',
                stage: { id: 8, name: 'Сборка' },
                replenishmentMethod: 'Закупка',
                qtyPerParent: 2,
                unit: 'шт',
                computed: { treeQty: 2 },
                warnings: ['NO_STOCK'],
              },
              {
                id: 'operation-4',
                parentId: 'item-100',
                type: 'operation',
                operation: { id: 4, name: 'Токарная операция' },
                stage: { id: 7, name: 'Мехобработка' },
                timeNormNh: 0.5,
                computed: { treeTimeNh: 0.5 },
              },
            ],
          }],
          meta: { root: pump },
        },
      })
      return
    }
    if (key === 'GET /api/v1/specification/flattened') {
      await route.fulfill({
        json: {
          items: [{
            item_id: 200,
            item_code: 'BEARING-01',
            article: 'ПД-01',
            name: 'Подшипник ведущего вала',
            unit: 'шт',
            replenishment_method: 'Закупка',
            total_qty: 2,
            occurrences: 1,
            levels: [1],
            stages: ['Сборка'],
            paths: [{ level: 1, qty: 2, path: 'Насос ГА-1 / Подшипник ведущего вала' }],
            warnings: ['NO_STOCK'],
          }],
          meta: { root: pump, count: 1, root_qty: 1 },
        },
      })
      return
    }
    if (key === 'GET /api/v1/specification/where-used') {
      await route.fulfill({
        json: {
          items: [{
            parent: {
              ...pump,
              item_id: 101,
              item_code: 'UNIT-01',
              item_name: 'Насосная установка',
              item_article: 'УСТ-01',
              spec_id: 11,
            },
            spec: { spec_id: 11, spec_name: 'СП-11' },
            component_item_id: 100,
            qty_per_parent: 1,
            total_qty_to_target: 1,
            level_up: 1,
            stage: { id: 8, name: 'Сборка' },
            path: [{ item_id: 101, name: 'Насосная установка' }],
          }],
          meta: { target: pump, count: 1, max_depth: 10 },
        },
      })
      return
    }
    if (key === 'GET /api/v1/specification/quality') {
      await route.fulfill({
        json: {
          issues: [{
            code: 'NO_STOCK',
            severity: 'warning',
            message: 'Недостаточный остаток компонента',
            item: {
              item_id: 200,
              item_code: 'BEARING-01',
              item_article: 'ПД-01',
              item_name: 'Подшипник ведущего вала',
            },
            spec_id: 10,
          }],
          meta: { root: pump, count: 1 },
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

test('specification loaded tree cockpit visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await mockSpecificationApi(page)

  await page.goto('/#/specification')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await page.getByPlaceholder('Артикул, код, название').fill('НАС-01')
  await page.getByRole('button', { name: 'Найти' }).click()
  await expect(page.getByText('Загружено: НАС-01 · Насос ГА-1')).toBeVisible()
  await expect(page.locator('.bomSummaryStrip')).toContainText('Узлы3всего')
  await expect(page.getByRole('row', { name: /Подшипник ведущего вала/ })).toBeVisible()

  await page.getByRole('row', { name: /Подшипник ведущего вала/ }).click()
  await expect(page.locator('.bomDetailPane')).toContainText('Подшипник ведущего вала')
  await expect(page.locator('.bomDetailPane')).toContainText('NO_STOCK')
  await expect(page.locator('.statusBar')).toContainText('Строки 1-3 из 3')

  await expect(page.locator('.app')).toHaveScreenshot('specification-tree-cockpit.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})
