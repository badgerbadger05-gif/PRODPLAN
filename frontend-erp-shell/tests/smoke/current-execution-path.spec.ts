import { expect, test, type Page } from '@playwright/test'

const plan = {
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
  planned_output_qty: 10,
  accepted_plan_output_qty: 4,
  assembly_remaining_qty: 6,
  plan_output_truth_status: 'accepted',
  plan_output_truth_reason: null,
  plan_output_generation_id: 77,
  plan_output_cutoff: '2026-07-20T12:00:00Z',
}

const stableMrpIdentity = 'mrp-run:900:production:requirement:1:item:501:allocation:default'
const queueIdentity = 'plan-line:502'

function matrixResponse() {
  return {
    plan,
    planned_output_qty: 10,
    accepted_plan_output_qty: 4,
    assembly_remaining_qty: 6,
    plan_output_truth_status: 'accepted',
    plan_output_truth_reason: null,
    plan_output_generation_id: 77,
    plan_output_cutoff: '2026-07-20T12:00:00Z',
    buckets: ['2026-05-01'],
    bucket_totals: { '2026-05-01': 10 },
    rows: [{
      item_id: 501,
      item_code: 'C-501',
      item_name: 'Насос ГА-1',
      item_article: 'ART-501',
      total_qty: 10,
      planned_output_qty: 10,
      accepted_plan_output_qty: 4,
      assembly_remaining_qty: 6,
      output_by_bucket: {
        '2026-05-01': { planned_output_qty: 10, accepted_plan_output_qty: 4, assembly_remaining_qty: 6 },
      },
      buckets: { '2026-05-01': 10 },
      locked_buckets: {},
    }],
    total_qty: 10,
    grand_total: 10,
    total: 10,
  }
}

function journalResponse() {
  return {
    plan,
    run_id: 900,
    total: 1,
    limit: 100,
    offset: 0,
    truth_status: 'accepted',
    truth_reason: null,
    reason: null,
    summary: {
      truth_status: 'accepted',
      total_items: 1,
      planned_output_qty: 10,
      accepted_plan_output_qty: 4,
      assembly_remaining_qty: 6,
      fully_covered: 0,
      partially_covered: 1,
      not_covered: 0,
      net_zero: 0,
      execution_by_flow: {
        production: { completed_qty: 4, base_qty: 10, execution_pct: 40, available: true },
      },
    },
    facets: { bom_levels: [0], flows: ['production'], statuses: ['partial'] },
    rows: [{
      req_id: 1,
      item_id: 501,
      item_code: 'C-501',
      item_name: 'Насос ГА-1',
      item_article: 'ART-501',
      flow: 'production',
      bom_level: 0,
      gross_qty: 10,
      net_qty: 10,
      ordered_qty: 4,
      completed_qty: 4,
      covered_qty: 4,
      remaining_qty: 6,
      unassigned_qty: 6,
      coverage_pct: 40,
      status: 'partial',
      status_label: 'Частично',
      explanations: ['Основание сохранено backend-публикацией'],
      current_identity: 'period-plan:123:requirement:1:item:501',
      source_revision: 'period-plan:123:accepted:77',
      need_date: '2026-05-15',
      execution_available: true,
      execution_unavailable_reason: null,
      ledger_links: null,
      basis_links: {
        item: { label: 'Ledger item', href: '#/ledger/items/501?tab=movements', available: true, reason: null },
        reservations: [{ label: 'Ledger reservation', href: '#/ledger/items/501?tab=reservations&reservation_id=101', available: true, reason: null }],
        events: [{ label: 'Ledger event', href: '#/ledger/items/501?tab=reservations&reservation_id=101&event_id=11', available: true, reason: null }],
        reason: null,
      },
      queue_links: [{
        label: 'Assembly queue',
        href: '#/production-control?view=assembly-queue&current_identity=plan-line%3A502',
        available: true,
        reason: null,
        current_identity: queueIdentity,
        source_revision: 'accepted:g77:assembly_queue',
      }],
      queue_link_reason: null,
      root_item_ids: [501],
      reservation_ids: [101],
      execution_events: [],
      execution_allocations: [],
      information_links: null,
      work_items: [{
        type: 'planned_order',
        order_id: 77,
        qty: 4,
        assigned_qty: 4,
        unassigned_qty: 0,
        current_identity: stableMrpIdentity,
        navigation_href: `#/mrp-runs/900?tab=production&current_identity=${encodeURIComponent(stableMrpIdentity)}`,
        navigation_reason: null,
        remaining_qty: 6,
        need_date: '2026-05-15',
        forecast_date: null,
        forecast_shift_days: null,
        forecast_reason: null,
      }],
    }],
  }
}

function mrpSummary() {
  return {
    snapshot_id: 901,
    current_identity: 'mrp-run:900',
    source_revision: 'accepted:g77',
    ledger_generation: 77,
    cutoff: '2026-07-20T08:31:00+00:00',
    truth_status: 'accepted',
    truth_reason: null,
    run: {
      run_id: 900,
      status: 'SUCCESS',
      started_at: '2026-07-20T08:30:00Z',
      finished_at: '2026-07-20T08:31:00Z',
      horizon_days: 30,
      source_plan_id: 123,
    },
    counts: { production_orders: 1, purchase_requests: 0, rework_requests: 0 },
    capacity: { overloaded_buckets: 0, overload_total: 0 },
    snapshot_total_qty: { production: 4, purchase: 0, rework: 0, capacity: 0 },
  }
}

async function mockCriticalPathApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const { pathname } = url

    if (pathname.endsWith('/period-plans/123/matrix') || pathname.endsWith('/period-plans/7/matrix')) {
      await route.fulfill({ json: matrixResponse() })
      return
    }
    if (pathname.endsWith('/period-plans/123/runs')) {
      await route.fulfill({ json: { rows: [], total: 0 } })
      return
    }
    if (pathname.endsWith('/period-plans/123/execution-journal')) {
      await route.fulfill({ json: journalResponse() })
      return
    }
    if (pathname === '/api/v1/plan/results/900') {
      await route.fulfill({ json: mrpSummary() })
      return
    }
    if (pathname === '/api/v1/plan/results/900/production') {
      await route.fulfill({ json: {
        ...mrpSummary(),
        rows: [{
          current_identity: stableMrpIdentity,
          source_revision: 'accepted:g77',
          order_id: 1001,
          source_order_ids: [77],
          item_id: 501,
          item_name: 'Насос ГА-1',
          item_article: 'ART-501',
          unit: 'шт',
          qty: 4,
          need_date: '2026-05-15',
          start_date: '2026-05-10',
          finish_date: '2026-05-15',
          main_area_name: 'Сборка',
          norm_hours_total: 2,
        }],
        total: 1,
        limit: 200,
        offset: 0,
      } })
      return
    }
    if (pathname === '/api/v1/item-ledger/501/position') {
      await route.fulfill({ json: {
        item_id: 501, item_code: 'C-501', item_name: 'Насос ГА-1', pool_key: '501::default',
        on_hand: 4, on_hand_by_warehouse: [], incoming_supplier: 0, incoming_wip: 0, incoming: 0,
        reserved_soft: 1, available: 3, projected: 3, uncovered: 6,
        flags: { on_hand_negative: false, has_uncovered: true, reconcile_pending: false },
        truth_meta: { ledger_generation: 77, cutoff: '2026-07-20T08:31:00Z', truth_status: 'accepted', truth_reason: null },
      } })
      return
    }
    if (pathname === '/api/v1/item-ledger/501/reservations') {
      await route.fulfill({ json: { rows: [{
        reservation_id: 101, run_id: 900, plan_id: 123, plan_name: 'МАЙ 2026', requirement_id: 1,
        realization_mode: 'consume', priority: { period_from: '2026-05-01', period_to: '2026-05-29' },
        reserved_qty: 6, covered_from_stock_at_freeze_qty: 0, replenishment_required_qty: 6,
        replenishment_received_qty: 0, replenishment_remaining_qty: 6, lifecycle_status: 'active', allocations: [],
      }], truth_meta: { ledger_generation: 77, cutoff: '2026-07-20T08:31:00Z', truth_status: 'accepted', truth_reason: null } } })
      return
    }
    if (pathname === '/api/v1/item-ledger/501/reservations/101/events') {
      await route.fulfill({ json: { reservation_id: 101, rows: [{
        id: 11, event_at: '2026-07-20T08:00:00Z', event_kind: 'open', reserved_delta: 6, realized_delta: 0,
        sle_id: null, fact_ref: 'period-plan:123', fact_line_ref: '1', match_rule: 'addressed', cycle_id: 'cycle-101',
      }], truth_meta: { ledger_generation: 77, cutoff: '2026-07-20T08:31:00Z', truth_status: 'accepted', truth_reason: null } } })
      return
    }
    if (pathname === '/api/v1/item-ledger/501/future-supply' || pathname === '/api/v1/production-control/orders/root-products') {
      await route.fulfill({ json: { rows: [] } })
      return
    }
    if (pathname === '/api/v1/resources/') {
      await route.fulfill({ json: [] })
      return
    }
    if (pathname === '/api/v1/production-control/assembly-queue') {
      await route.fulfill({ json: {
        rows: [{
          plan_id: 123, plan_line_id: 502, run_id: 900, item_id: 501, item_code: 'C-501', item_name: 'Насос ГА-1',
          bucket_date: '2026-05-01', period_from: '2026-05-01', period_to: '2026-05-29',
          planned_output_qty: 10, accepted_plan_output_qty: 4, assembly_remaining_qty: 6,
          priority_key: ['2026-05-01', 502], sort_key: '2026-05-01|502', eligible_from: '2026-05-01',
          current_identity: queueIdentity, source_revision: 'accepted:g77:assembly_queue',
        }],
        total_rows: 1, total_queue_qty: 6, limit: 100, offset: 0,
        truth_meta: { ledger_generation: 77, cutoff: '2026-07-20T08:31:00Z', truth_status: 'accepted', truth_reason: null },
      } })
      return
    }
    await route.fulfill({ status: 200, json: { rows: [], total: 0 } })
  })
}

test('follows persisted plan → MRP → journal basis → assembly queue links', async ({ page }) => {
  await mockCriticalPathApi(page)
  await page.goto('/#/period-plan/123')

  await expect(page.getByRole('heading', { name: 'МАЙ 2026' })).toBeVisible()
  await page.getByRole('button', { name: 'Журнал исполнения' }).click()
  await expect(page.getByText('Насос ГА-1', { exact: true })).toBeVisible()

  await page.getByText('Насос ГА-1', { exact: true }).click()
  await page.getByRole('link', { name: 'Задание #77' }).click()
  await expect(page).toHaveURL(/#\/mrp-runs\/900\?tab=production&current_identity=/)
  await expect(page.getByText('Насос ГА-1', { exact: true })).toBeVisible()
  await expect(page.locator('tr.activeRow')).toContainText('Насос ГА-1')

  await page.goBack()
  await expect(page.getByRole('button', { name: 'Журнал исполнения' })).toBeVisible()
  await page.getByRole('button', { name: 'Журнал исполнения' }).click()
  await page.getByText('Насос ГА-1', { exact: true }).click()
  await page.getByRole('link', { name: 'Ledger event' }).click()
  await expect(page).toHaveURL(/#\/ledger\/items\/501\?tab=reservations&reservation_id=101&event_id=11/)
  await expect(page.getByRole('heading', { name: 'События резерва #101' })).toBeVisible()
  await expect(page.locator('.ledgerTimelineStep.selected')).toContainText('Открыт')

  await page.goBack()
  await expect(page.getByRole('button', { name: 'Журнал исполнения' })).toBeVisible()
  await page.getByRole('button', { name: 'Журнал исполнения' }).click()
  await page.getByText('Насос ГА-1', { exact: true }).click()
  await page.getByRole('link', { name: 'Assembly queue' }).click()
  await expect(page).toHaveURL(/#\/production-control\?view=assembly-queue&current_identity=plan-line%3A502/)
  await expect(page.getByRole('heading', { name: 'Очередь сборки' })).toBeVisible()
  const highlightedQueueRow = page.getByTestId('assembly-queue-row-plan-line:502')
  await expect(highlightedQueueRow).toHaveClass('activeRow')
  await expect(highlightedQueueRow.getByText('accepted:g77:assembly_queue')).toBeVisible()
})
