import { expect, test, type Page } from '@playwright/test'

const positions = [
  {
    id: 1, item_id: 100, item_code: 'PUMP-01', item_name: 'Насос ГА-1', warehouse_ref1c: 'MAIN',
    supply_type: 'purchase', mode: 'shelf', adu: 2, commonality: 1, red_qty: 4, yellow_qty: 6,
    green_qty: 8, target_qty: 18, data_quality: [], source_schedule_id: 47, is_active: true, is_stale: false,
    calculated_at: '2026-07-21T08:30:00Z',
    live_nfp: {
      stock_qty: 3, open_supply_qty: 2, qualified_demand_qty: 9, nfp: -4, zone: 'red', penetration: 1.22,
      is_complete: true, missing_reasons: [], data_quality: [], formula: '3 + 2 - 9',
      timestamps: { stock_as_of: '2026-07-21T08:00:00Z', supply_as_of: '2026-07-21T08:05:00Z' },
    },
  },
  {
    id: 2, item_id: 101, item_code: 'GEAR-01', item_name: 'Редуктор', warehouse_ref1c: 'ASSEMBLY',
    supply_type: 'manufacture', mode: 'under_schedule', adu: 1.5, commonality: 2, red_qty: 3, yellow_qty: 5,
    green_qty: 7, target_qty: 15, data_quality: [], source_schedule_id: 47, is_active: true, is_stale: false,
    calculated_at: '2026-07-21T08:30:00Z',
    live_nfp: {
      stock_qty: 7, open_supply_qty: 4, qualified_demand_qty: 5, nfp: 6, zone: 'yellow', penetration: 0.6,
      is_complete: true, missing_reasons: [], data_quality: [], formula: '7 + 4 - 5',
      timestamps: { stock_as_of: '2026-07-21T08:00:00Z', supply_as_of: '2026-07-21T08:05:00Z' },
    },
  },
]

const signals = [
  {
    id: 201, dedup_key: 'purchase-201', signal_type: 'Пополнение', position_id: 1, item_id: 100,
    item_code: 'PUMP-01', item_name: 'Насос ГА-1', warehouse_ref1c: 'MAIN', status: 'Open',
    suggested_qty: 12, priority: 1.25, zone: 'red', nfp_snapshot: -4, target_qty_snapshot: 18,
    kit_force: false, kit_shortage_qty: 0, can_launch: false, deficit_lines: [], data_quality: [],
    is_incomplete: false, refreshed_at: '2026-07-21T08:30:00Z',
  },
  {
    id: 202, dedup_key: 'production-202', signal_type: 'Под график', position_id: 2, item_id: 101,
    item_code: 'GEAR-01', item_name: 'Редуктор', warehouse_ref1c: 'ASSEMBLY', status: 'Open',
    suggested_qty: 6, priority: 2.5, zone: 'yellow', kit_force: true, kit_shortage_qty: 4,
    drum_slot_id: 103, need_date: '2026-07-21', required_date: '2026-07-23', raw_demand_qty: 10,
    raw_shortage_qty: 4, material_status: 'Частично', kit_cls: 'part', can_launch: true,
    deficit_lines: [{
      item: 'BEARING-6205', item_name: 'Подшипник 6205', article: '6205', need: 6, have: 2,
      gross: 2, kind: 'buy', level: '1', cls: 'part', buffered: false,
    }],
    root_items: [{ item: 'GEAR-01', item_name: 'Редуктор', article: 'GEAR-01' }],
    data_quality: [], is_incomplete: false, refreshed_at: '2026-07-21T08:30:00Z',
  },
]

async function mockDbrFeederApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const key = `${request.method()} ${url.pathname}`

    if (key === 'GET /api/v1/dbr/feeder/positions') {
      expect(Object.fromEntries(url.searchParams)).toEqual({ include_live_nfp: 'true', active_only: 'true', limit: '5000' })
      await route.fulfill({ json: positions })
      return
    }
    if (key === 'GET /api/v1/dbr/feeder/signals') {
      expect(Object.fromEntries(url.searchParams)).toEqual({ status: 'Open', limit: '5000' })
      await route.fulfill({ json: signals })
      return
    }
    if (key === 'GET /api/v1/dbr/feeder/deficits') {
      await route.fulfill({ json: {
        deficits: [{ item: 'BEARING-6205', item_name: 'Подшипник 6205', article: '6205', source: 'buy', short_qty: 4, need_sum: 6, gross: 2, blocks_signals: 1, nearest_due: '2026-07-23' }],
        kpis: { deficit_materials: 1, queue_open: 2, stock_source: 'selected - ignored' },
      } })
      return
    }
    if (key === 'GET /api/v1/dbr/feeder/processing/board') {
      await route.fulfill({ json: {
        roundtrip_limit_days: 14, positions_total: 1, overdue_positions: 1, generated_at: '2026-07-21T08:30:00Z',
        positions: [{
          position_id: 3, item_id: 102, item_code: 'SHAFT-01', item_article: 'ВАЛ-01', item_name: 'Вал приводной',
          adu: 1, rt_days: 4, trip_interval_days: 7, red_qty: 2, yellow_qty: 3, target_qty: 9,
          nfp: 4, zone: 'yellow', penetration: 0.55, stock_qty: 2, open_supply_qty: 1, chain_supply_qty: 1,
          is_complete: true, missing_reasons: [], has_overdue: true,
          open_orders: [{ order_id: 81, line_id: 811, order_number: 'ПР-000081', order_date: '2026-07-01', transfer_date: '2026-07-03', report_date: null, stage: 'transferred', remaining_qty: 5, age_days: 18, overdue: true }],
        }],
      } })
      return
    }
    if (key === 'GET /api/v1/dbr/settings') {
      await route.fulfill({ json: { feeder_chain_enabled: false } })
      return
    }

    await route.fulfill({ status: 500, json: { detail: `Unexpected visual-test request: ${key}` } })
  })
}

test.use({
  viewport: { width: 1440, height: 1600 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('DBR feeder loaded cockpit visual baseline', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-21T09:00:00Z'))
  await mockDbrFeederApi(page)
  await page.goto('/#/dbr/feeder')
  await page.addStyleTag({ content: `*, *::before, *::after { animation: none !important; caret-color: transparent !important; transition: none !important; }` })

  await expect(page.getByRole('heading', { name: 'Позиции супермаркета' })).toBeVisible()
  await expect(page.getByLabel('Зона NFP')).toHaveValue('')
  await expect(page.getByLabel('Статус сигнала')).toHaveValue('Open')
  await expect(page.getByText('Сигналов: 2', { exact: true })).toBeVisible()
  await expect(page.getByRole('row', { name: /PUMP-01.*Насос ГА-1/ }).first()).toBeVisible()
  await expect(page.getByText('Дефицитных позиций: 1; открытых сигналов: 2')).toBeAttached()
  await expect(page.getByText('Позиций: 1; просрочен кругорейс (>14 дн): 1')).toBeAttached()
  await expect(page.locator('.dbrFeederKpis')).toContainText('Позиции2')
  await expect(page.locator('.dbrFeederTable')).toContainText('GEAR-01')
  await expect(page.locator('.statusBar')).toContainText('Строки 1-2 из 2')
  await expect(page.locator('.statusBar')).not.toContainText('Загрузка')

  await expect(page.locator('.app')).toHaveScreenshot('dbr-feeder-loaded.png', {
    animations: 'disabled', caret: 'hide', scale: 'css',
  })
})
