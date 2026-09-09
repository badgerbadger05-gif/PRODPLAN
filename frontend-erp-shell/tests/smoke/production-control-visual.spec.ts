import { expect, test } from '@playwright/test'

const orders = [
  {
    product_id: 101,
    item_id: 201,
    order_number: 'ORD-1',
    order_prodplan_number: 'ПП-000101',
    order_source: 'mrp',
    source: 'mrp',
    order_ref1c: null,
    order_date: '2026-07-18',
    line_number: 1,
    item_name: 'Кронштейн опорный',
    item_article: 'КР-01',
    unit: 'шт',
    quantity: 10,
    produced_qty: 0,
    remaining_qty: 10,
    planned_start_date: '2026-07-21',
    planned_finish_date: '2026-07-23',
    forecast_date: '2026-07-24',
    forecast_shift_days: 1,
    forecast_status: 'delayed',
    forecast_reason: 'Ожидание комплектующих',
    status: 'ready',
    coverage_status: 'assembled',
    coverage_label: 'Собрано',
    issue_status: 'posted',
    issue_count: 1,
    workshop_name: 'Сборочный участок',
    stage_name: 'Сборка',
    optimal_batch: 5,
    source_plan_id: 7,
    source_plan_name: 'План июля',
  },
  {
    product_id: 102,
    item_id: 202,
    order_number: 'ЗСНФ-000202',
    order_prodplan_number: 'ПП-000102',
    order_source: '1c',
    source: '1c',
    order_ref1c: 'order-ref-102',
    order_one_c_number: 'ЗСНФ-000202',
    item_name: 'Вал приводной',
    item_article: 'ВП-02',
    unit: 'шт',
    quantity: 5,
    produced_qty: 0,
    remaining_qty: 5,
    planned_start_date: '2026-07-22',
    planned_finish_date: '2026-07-25',
    forecast_date: '2026-08-02',
    forecast_shift_days: 8,
    forecast_status: 'critical',
    forecast_reason: 'Дефицит подшипников',
    status: 'shortage',
    coverage_status: 'shortage',
    coverage_label: 'Дефицит',
    workshop_name: 'Механический участок',
    stage_name: 'Мехобработка',
  },
]

const materials = {
  order_number: 'ORD-1',
  item_name: 'Кронштейн опорный',
  coverage_status: 'assembled',
  coverage_label: 'Собрано',
  components: [{
    component_item_id: 301,
    item_name: 'Болт М8',
    item_article: 'BOLT-8',
    qty_per_unit: 4,
    available_qty: 40,
    required_qty: 40,
    missing_qty: 0,
    unit: 'шт',
    availability_status: 'assembled',
    coverage_status: 'assembled',
    coverage_label: 'Собрано',
  }],
}

const drumBlocker = {
  item_id: 940,
  item_code: 'CA-004940-SP',
  item_article: 'CA-004940-SP',
  item_name: 'Конёк для лыжи, чёрный',
  required_qty: '10',
  available_qty: '6',
  shortage_qty: '4',
  reason: 'SHORTAGE',
  destination_warehouse_ref1c: 'Склад участка модулей',
  path: [201, 940],
  point_of_use_qty: '1',
  custody_qty: '1',
  transit_qty: '1',
  wip_qty: '2',
  supplier_qty: '1',
  other_stock_qty: '3',
  coverage_sources: [
    { coverage_kind: 'point_of_use', qty: '1', source_key: 'stock:assembly:940', warehouse_ref1c: 'Склад участка модулей', destination_warehouse_ref1c: 'Склад участка модулей', available_date: null, confidence: 'physical', source_kind: 'physical_stock', source_ref: 'Склад участка модулей' },
    { coverage_kind: 'custody', qty: '1', source_key: 'custody:91', warehouse_ref1c: 'Участок сборки модулей', destination_warehouse_ref1c: 'Склад участка модулей', available_date: null, confidence: 'custody', source_kind: 'custody_workshop', source_ref: 'Заказ ЗСНФ-002926' },
    { coverage_kind: 'transit', qty: '1', source_key: 'custody:92', warehouse_ref1c: 'Склад №3', destination_warehouse_ref1c: 'Склад участка модулей', available_date: '2026-09-07', confidence: 'custody', source_kind: 'custody_transit', source_ref: 'Перемещение ПМ-000118' },
    { coverage_kind: 'wip_order', qty: '2', source_key: 'future:31', warehouse_ref1c: 'Склад участка модулей', destination_warehouse_ref1c: 'Склад участка модулей', available_date: '2026-09-08', confidence: 'committed', source_kind: 'wip_order', source_ref: 'ЗСНФ-002944' },
    { coverage_kind: 'supplier_order', qty: '1', source_key: 'future:32', warehouse_ref1c: 'Склад участка модулей', destination_warehouse_ref1c: 'Склад участка модулей', available_date: '2026-09-09', confidence: 'committed', source_kind: 'supplier_order', source_ref: 'ЗП-000042' },
    { coverage_kind: 'other_stock', qty: '3', source_key: 'stock:store-4:940', warehouse_ref1c: 'Склад №4', destination_warehouse_ref1c: 'Склад участка модулей', available_date: null, confidence: 'physical', source_kind: 'physical_stock', source_ref: 'Склад №4' },
  ],
}

const drumSchedule = {
  schedule_from: '2026-09-04',
  schedule_to: '2026-09-10',
  days: ['2026-09-04', '2026-09-07', '2026-09-08', '2026-09-09', '2026-09-10'],
  resources: [
    { resource_id: 17, resource_name: 'Участок сборки модулей' },
    { resource_id: 18, resource_name: 'Участок сборки мотобуксировщиков' },
    { resource_id: 19, resource_name: 'Участок сборки снегоходов' },
  ],
  slots: [{
    slot_id: 501,
    queue_line_id: 901,
    run_id: 77,
    plan_id: 7,
    plan_line_id: 71,
    period_from: '2026-07-01',
    period_to: '2026-07-31',
    item_id: 201,
    item_code: 'MODULE-FISHRIDE',
    item_name: 'Лыжный модуль Fishride NEW',
    resource_id: 17,
    slot_date: '2026-09-04',
    auto_slot_date: '2026-09-07',
    slot_qty: 3,
    planned_output_qty: 10,
    accepted_plan_output_qty: 0,
    assembly_remaining_qty: 10,
    slot_ordinal: 0,
    readiness_phase: 'transfer',
    readiness_date: '2026-09-07',
    readiness_curve: [
      { horizon: 'now', cumulative_qty: '1', available_date: '2026-09-04', actions: [], required_actions: [], blockers: [drumBlocker] },
      { horizon: 'transfer', cumulative_qty: '3', available_date: '2026-09-07', actions: [{ action_kind: 'transfer', item_id: 940, item_code: 'CA-004940-SP', item_article: 'CA-004940-SP', item_name: 'Конёк для лыжи, чёрный', qty: '2', available_date: '2026-09-07', confidence: 'physical', source_key: 'stock:store-3:940', source_warehouse_ref1c: 'Склад №3', destination_warehouse_ref1c: 'Склад участка модулей', resource_id: 17, path: [201] }], required_actions: [], blockers: [drumBlocker] },
      { horizon: 'kitting', cumulative_qty: '3', available_date: '2026-09-07', actions: [], required_actions: [], blockers: [drumBlocker] },
      { horizon: 'committed', cumulative_qty: '3', available_date: '2026-09-08', actions: [], required_actions: [], blockers: [drumBlocker] },
      { horizon: 'launch', cumulative_qty: '3', available_date: '2026-09-08', actions: [], required_actions: [{ action_kind: 'make', item_id: 940, item_code: 'CA-004940-SP', item_article: 'CA-004940-SP', item_name: 'Конёк для лыжи, чёрный', qty: '4', available_date: '2026-09-11', confidence: 'forecast', source_key: '', source_warehouse_ref1c: '', destination_warehouse_ref1c: 'Склад участка модулей', resource_id: 17, path: [201] }], blockers: [drumBlocker] },
    ],
    action_manifest: [
      { action_kind: 'transfer', item_id: 940, item_code: 'CA-004940-SP', item_article: 'CA-004940-SP', item_name: 'Конёк для лыжи, чёрный', qty: '2', available_date: '2026-09-07', confidence: 'physical', source_key: 'stock:store-3:940', source_warehouse_ref1c: 'Склад №3', destination_warehouse_ref1c: 'Склад участка модулей', resource_id: 17, path: [201] },
      { action_kind: 'make', item_id: 940, item_code: 'CA-004940-SP', item_article: 'CA-004940-SP', item_name: 'Конёк для лыжи, чёрный', qty: '4', available_date: '2026-09-11', confidence: 'forecast', source_key: '', source_warehouse_ref1c: '', destination_warehouse_ref1c: 'Склад участка модулей', resource_id: 17, path: [201] },
    ],
    unavailable_reasons: [],
    blocking_manifest: [drumBlocker],
    manual_override: true,
    manual_moved_at: '2026-09-04T11:25:00+03:00',
    manual_moved_by: 'Мастер сборки',
    original_priority: ['2026-07-01', 71],
  }],
  gaps: [],
  excluded: [],
  total_open_qty: 10,
  total_slot_qty: 3,
  total_gap_qty: 7,
  total_slots: 1,
  total_gaps: 0,
  total_excluded: 0,
  total_excluded_open_qty: 0,
  limit: 10000,
  offset: 0,
  truth_meta: {
    ledger_generation: 890,
    cutoff: '2026-09-04T09:30:00+03:00',
    truth_status: 'accepted',
    truth_reason: null,
  },
}

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('production control visual contract', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-20T12:00:00Z'))
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/production-control/orders') {
      await route.fulfill({
        json: {
          rows: orders,
          total: orders.length,
          limit: 100,
          offset: 0,
          latest_run_id: 77,
          truth_meta: {
            ledger_generation: 77,
            cutoff: '2026-07-31T00:00:00Z',
            truth_status: 'accepted',
            truth_reason: null,
          },
        },
      })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({
        json: [
          { resource_id: 1, resource_name: 'Сборочный участок' },
          { resource_id: 2, resource_name: 'Механический участок' },
        ],
      })
      return
    }
    if (pathname === '/api/v1/plan/period-plans') {
      await route.fulfill({ json: { rows: [], total: 0, limit: 500, offset: 0 } })
      return
    }
    if (pathname === '/api/v1/production-control/orders/101/materials') {
      await route.fulfill({ json: materials })
      return
    }
    await route.abort('failed')
  })

  await page.goto('/#/production-control')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  await expect(page.getByRole('heading', { name: 'Журнал заказов на производство' })).toBeVisible()
  await expect(page.getByText('Кронштейн опорный').first()).toBeVisible()
  await expect(page.getByText('Вал приводной')).toBeVisible()
  await expect(page.getByText('Болт М8')).toBeVisible()
  await expect(page.locator('.runBadge')).toHaveText('MRP run: 77')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')
  const firstOrderRow = page.getByRole('row').filter({ hasText: 'Кронштейн опорный' }).first()
  await expect(firstOrderRow.getByLabel('Факт выпуска заказа')).toContainText('Заказано 10')
  await expect(firstOrderRow.getByLabel('Факт выпуска заказа')).toContainText('Принято Ledger 0')
  await expect(firstOrderRow.getByLabel('Факт выпуска заказа')).toContainText('Осталось по заказу 10')
  const selectedCardFacts = page.locator('.detailPane').getByLabel('Факт выпуска заказа')
  await expect(selectedCardFacts).toContainText('Принято Ledger 0')

  await expect(page.locator('.app')).toHaveScreenshot('production-control.png', {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  })
})

test('drum master card renders saved readiness evidence and scrolls', async ({ page }, testInfo) => {
  await page.clock.setFixedTime(new Date('2026-09-04T09:30:00+03:00'))
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/production-control/drum') {
      await route.fulfill({ json: drumSchedule })
      return
    }
    if (pathname === '/api/v1/production-control/orders') {
      await route.fulfill({ json: { rows: [], total: 0, limit: 100, offset: 0, latest_run_id: null, truth_meta: drumSchedule.truth_meta } })
      return
    }
    if (pathname === '/api/v1/production-control/orders/root-products') {
      await route.fulfill({ json: { rows: [], total: 0 } })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({ json: drumSchedule.resources })
      return
    }
    await route.fulfill({ status: 404, json: { detail: `Not mocked: ${pathname}` } })
  })

  await page.goto('/#/production-control?view=drum')
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        caret-color: transparent !important;
        transition: none !important;
      }
    `,
  })

  const tile = page.getByRole('button', { name: /Лыжный модуль Fishride NEW: 3 шт., После перемещения/ })
  await expect(tile).toBeVisible()
  await expect(tile).toContainText('Исходный план 10')
  await expect(tile).toContainText('Принято Ledger 0')
  await expect(tile).toContainText('Осталось выпустить 10')
  await expect(tile).toContainText('Сегодня можно 1 из 3')
  await expect(tile).toContainText('Нет: CA-004940-SP, −4')
  await tile.click()

  const dialog = page.getByRole('dialog', { name: /Плитка: Лыжный модуль Fishride NEW/ })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('Факт выпуска плана')).toContainText('Исходный план 10')
  await expect(dialog.getByLabel('Факт выпуска плана')).toContainText('Принято Ledger 0')
  await expect(dialog.getByLabel('Факт выпуска плана')).toContainText('Осталось выпустить 10')
  await expect(dialog.getByRole('table', { name: 'Дефициты по источникам обеспечения' })).toBeVisible()
  await expect(dialog.getByText('Сегодня можно собрать 1 из 3')).toBeVisible()
  await expect(dialog.getByText('MRP: 77')).toBeVisible()
  await expect(dialog.getByText('Период: 2026-07-01 — 2026-07-31')).toBeVisible()
  await expect(dialog.getByText('Перемещение ПМ-000118')).toBeVisible()
  await expect(dialog.getByText('ЗСНФ-002944')).toBeVisible()
  await expect(dialog.getByText('ЗП-000042')).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Журнал' })).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Очередь мехцеха' })).toBeVisible()

  const body = dialog.locator('.dialogBody')
  await expect.poll(async () => body.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true)
  await body.evaluate((node) => { node.scrollTop = node.scrollHeight })
  await expect.poll(async () => body.evaluate((node) => node.scrollTop > 0)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('drum-deficit-card.png'), fullPage: true })
})

test('drum shows yellow material feasibility without production orders', async ({ page }, testInfo) => {
  const action = { ...drumSchedule.slots[0].action_manifest[1], confidence: 'required', available_date: '2026-09-04' }
  const slot = {
    ...drumSchedule.slots[0],
    readiness_phase: 'launch', readiness_date: '2026-09-04', manual_override: false,
    blocking_manifest: [], action_manifest: [action],
    readiness_curve: ['now', 'transfer', 'kitting', 'committed', 'launch'].map((horizon) => ({
      horizon, cumulative_qty: horizon === 'launch' ? '3' : '0',
      available_date: horizon === 'launch' ? '2026-09-04' : null,
      actions: horizon === 'launch' ? [action] : [], required_actions: [], blockers: [],
    })),
  }
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/production-control/drum') {
      await route.fulfill({ json: { ...drumSchedule, slots: [slot] } })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({ json: drumSchedule.resources })
      return
    }
    await route.fulfill({ json: { rows: [], total: 0, truth_meta: drumSchedule.truth_meta } })
  })
  await page.goto('/#/production-control?view=drum')
  const tile = page.getByRole('button', { name: /Лыжный модуль Fishride NEW: 3 шт., Материалы есть/ })
  await expect(tile).toBeVisible()
  await expect(tile).toHaveCSS('background-color', 'rgb(254, 243, 199)')
  await expect(tile).toContainText('Изготовить узлы 3 из 3')
  await expect(tile).toContainText('материалы на 2026-09-04')
  await tile.click()
  const dialog = page.getByRole('dialog', { name: /Плитка: Лыжный модуль Fishride NEW/ })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('Конёк для лыжи, чёрный').first()).toBeVisible()
  await expect(dialog.getByText('Материалы есть для 3 из 3 — нужно изготовить узлы')).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Открыть узел в очереди мехцеха' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('drum-material-feasibility.png'), fullPage: true })
})

test('mechshop keeps drum work visible after MRP execution is closed', async ({ page }) => {
  const row = {
    ...orders[0], journal_row_key: 'work-item:501', work_item_id: 501,
    product_id: null, order_id: null, source: 'mrp', order_source: 'mrp',
    quantity: 0, remaining_qty: 0, launchable_qty: 0,
    readiness_required_qty: 4, readiness_need_date: '2026-09-04',
    launch_source: 'drum_readiness', available_actions: [],
    selection_disabled_reason: 'Узел нужен барабану; незапущенного остатка MRP нет.',
  }
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === '/api/v1/production-control/orders') {
      await route.fulfill({ json: { rows: [row], total: 1, limit: 100, offset: 0, truth_meta: drumSchedule.truth_meta } })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({ json: [] })
      return
    }
    if (pathname.endsWith('/materials')) {
      await route.fulfill({ json: materials })
      return
    }
    await route.abort('failed')
  })
  await page.goto('/#/production-control?view=mechshop&planning_contour=mrp&launch_source=drum_readiness')
  const hint = page.getByRole('row').filter({ hasText: 'Для сборки: 4' })
  await expect(hint).toBeVisible()
  await expect(hint.getByRole('checkbox')).toBeDisabled()
  await expect(hint).toContainText('Кронштейн опорный')
})

test('standalone piecework selects welding operations without production', async ({ page }, testInfo) => {
  let command: Record<string, unknown> | null = null
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/piecework-options')) {
      await route.fulfill({ json: { product_id: 501, item_name: 'Опора, после сварки', quantity: 10, operations: [
        { spec_operation_id: 51, operation_id: 61, line_number: 1, operation_name: 'Сварка' },
        { spec_operation_id: 52, operation_id: 62, line_number: 2, operation_name: 'Зачистка' },
      ] } })
    } else if (path.endsWith('/piecework')) {
      command = route.request().postDataJSON()
      await route.fulfill({ json: { status: 'ok', message: 'Сдельный наряд оформлен', product_id: 501, command_id: 1, created: 1 } })
    } else if (path.endsWith('/orders')) {
      await route.fulfill({ json: { rows: [{ ...orders[0], order_ref1c: 'order-ref-101', available_actions: ['produce'] }], total: 1,
        truth_meta: { ledger_generation: 77, truth_status: 'accepted', cutoff: '2026-09-09T00:00:00Z' } } })
    } else if (path.endsWith('/employees')) {
      await route.fulfill({ json: { rows: [{ employee_id: 1, employee_ref1c: 'E1', employee_type: 'employee', employee_name: 'Иванов' }], total: 1 } })
    } else if (path.endsWith('/materials')) {
      await route.fulfill({ json: materials })
    } else if (path.endsWith('/resources/')) {
      await route.fulfill({ json: [] })
    } else if (path.endsWith('/period-plans')) {
      await route.fulfill({ json: { rows: [], total: 0 } })
    } else {
      await route.abort('failed')
    }
  })
  await page.goto('/#/production-control')
  await page.getByRole('row').filter({ hasText: 'Кронштейн опорный' }).first().getByRole('checkbox').check()
  const button = page.getByRole('button', { name: 'Сдельный наряд', exact: true })
  await expect(button).toHaveCSS('background-color', 'rgb(255, 56, 164)')
  await button.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('Опора, после сварки')
  await dialog.getByRole('checkbox', { name: 'Оформить операцию Зачистка' }).uncheck()
  await dialog.getByRole('combobox').first().selectOption('E1')
  await dialog.getByRole('spinbutton').fill('6')
  await page.screenshot({ path: testInfo.outputPath('standalone-piecework.png') })
  await dialog.getByRole('button', { name: 'Создать сдельный наряд' }).click()
  await expect.poll(() => command).toMatchObject({ qty: 6, operation_executors: [{ spec_operation_id: 51, employee_ref1c: 'E1' }] })
  await expect(page.getByText('Сдельный наряд оформлен')).toBeVisible()
})

test('unified Produce sends actual quantity and partial intent', async ({ page }, testInfo) => {
  let command: Record<string, unknown> | null = null
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/produce')) {
      command = route.request().postDataJSON()
      await route.fulfill({ json: { qty: 7, message: 'Частичный выпуск оформлен, заказ открыт' } })
    } else if (path.endsWith('/orders')) {
      await route.fulfill({ json: { rows: [{ ...orders[0], order_ref1c: 'order-ref-101', available_actions: ['produce'] }], total: 1,
        truth_meta: { ledger_generation: 77, truth_status: 'accepted', cutoff: '2026-09-09T00:00:00Z' } } })
    } else if (path.endsWith('/materials')) {
      await route.fulfill({ json: materials })
    } else if (path.endsWith('/resources/')) {
      await route.fulfill({ json: [] })
    } else if (path.endsWith('/employees') || path.endsWith('/operations') || path.endsWith('/period-plans')) {
      await route.fulfill({ json: { rows: [], total: 0 } })
    } else {
      await route.abort('failed')
    }
  })
  await page.goto('/#/production-control')
  await page.getByRole('row').filter({ hasText: 'Кронштейн опорный' }).first().getByRole('checkbox').check()
  await expect(page.getByRole('button', { name: 'Закрыть в 1С', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Произвести', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('spinbutton').fill('7')
  await dialog.getByRole('checkbox', { name: 'Частичный выпуск' }).check()
  await page.screenshot({ path: testInfo.outputPath('unified-produce.png') })
  await dialog.getByRole('button', { name: 'Произвести', exact: true }).click()
  await expect.poll(() => command).toMatchObject({ qty: 7, partial: true, request_key: expect.any(String) })
  await expect(page.getByText('Частичный выпуск оформлен, заказ открыт')).toBeVisible()
})
