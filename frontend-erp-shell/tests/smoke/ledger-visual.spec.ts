import { expect, test } from '@playwright/test'

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  colorScheme: 'light',
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
})

test('item ledger read-only workspace', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-07-21T12:00:00Z'))
  await page.route('**/api/v1/item-ledger/9401/**', async (route) => {
    expect(route.request().method()).toBe('GET')
    const path = new URL(route.request().url()).pathname
    const bodies: Record<string, unknown> = {
      '/api/v1/item-ledger/9401/position': {
        item_id: 9401, item_code: '00000063', item_name: 'Труба профильная', pool_key: '9401::default',
        on_hand: 335.144, on_hand_by_warehouse: [{ warehouse_ref1c: 'wh-1', warehouse_name: 'Цех 1', qty: 335.144, qty_negative: false }],
        incoming_supplier: 120, incoming_wip: 0, incoming: 120, reserved_soft: 526.2,
        available: -191.056, projected: -71.056, uncovered: 71.056,
        flags: { on_hand_negative: false, has_uncovered: true, reconcile_pending: false },
      },
      '/api/v1/item-ledger/9401/movements': {
        total: 1, limit: 100, offset: 0,
        rows: [{ id: 88231, posting_at: '2026-07-21T09:40:03', warehouse_ref1c: 'wh-1', warehouse_name: 'Цех 1', qty: -40, qty_after: 295.144, movement_kind: 'assembly_out', record_type: 'Expense', recorder_type: 'Document_СборкаЗапасов', recorder_ref: '0129fc64', line_no: '2', ingest_source: 'document_pull', characteristic_ref: '', organization_ref: 'org' }],
      },
      '/api/v1/item-ledger/9401/reservations': { rows: [] },
      '/api/v1/item-ledger/9401/drift': { total: 0, limit: 100, offset: 0, rows: [] },
    }
    const body = bodies[path]
    if (!body) return route.fulfill({ status: 404, json: { detail: 'Not found' } })
    return route.fulfill({ status: 200, json: body })
  })

  await page.goto('/#/ledger/items/9401')
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;caret-color:transparent!important;transition:none!important}' })

  await expect(page.getByText('Труба профильная', { exact: true })).toBeVisible()
  await expect(page.getByText('00000063', { exact: true })).toBeVisible()
  await expect(page.getByRole('table', { name: 'Движения номенклатуры' })).toBeVisible()
  await expect(page.getByText('Сборка запасов', { exact: true })).toBeVisible()
  await expect(page.locator('.app')).toHaveScreenshot('ledger-workspace.png', {
    animations: 'disabled', caret: 'hide', scale: 'css',
  })
})
