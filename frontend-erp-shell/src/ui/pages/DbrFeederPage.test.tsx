import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  DbrFeederDeficitsResult,
  DbrFeederCockpit,
  DbrFeederPosition,
  DbrFeederSignal,
  DbrProcessingBoard,
  DbrPurchaseLaunchResult,
  DbrSignalLaunchResult,
} from '../../domain/dbr'
import { ApiError } from '../../lib/api'
import {
  getDbrFeederCockpit,
  getDbrFeederDeficits,
  getDbrFeederSignal,
  getDbrProcessingBoard,
  getDbrProcessingTripManifest,
  getDbrProcessingTripManifestPrint,
  getDbrSettings,
  launchDbrPurchase,
  launchDbrSignal,
  listDbrFeederPositions,
  listDbrFeederSignals,
  previewDbrFeederChain,
  previewDbrProcessingChain,
  previewDbrProcessingOrder,
  previewDbrFeederPositions,
  previewDbrFeederSignals,
  rebuildDbrFeederPositions,
  refreshDbrFeederChain,
  refreshDbrFeederSignals,
} from '../../services/dbr'
import { DbrFeederPage } from './DbrFeederPage'

vi.mock('../../services/dbr', () => ({
  dbrSnapshotUnavailableMessage: (error: unknown) => (
    error instanceof ApiError && error.status === 503
      ? 'dbr_cockpit_snapshot_unavailable: No DBR feeder cockpit snapshot for current accepted Ledger'
      : null
  ),
  getDbrFeederCockpit: vi.fn(),
  getDbrFeederDeficits: vi.fn(),
  getDbrFeederSignal: vi.fn(),
  getDbrProcessingBoard: vi.fn(),
  getDbrProcessingTripManifest: vi.fn(),
  getDbrProcessingTripManifestPrint: vi.fn(),
  getDbrSettings: vi.fn(),
  isDbrConflict: (error: unknown) => (
    typeof error === 'object' && error !== null && 'status' in error && error.status === 409
  ),
  launchDbrPurchase: vi.fn(),
  launchDbrSignal: vi.fn(),
  listDbrFeederPositions: vi.fn(),
  listDbrFeederSignals: vi.fn(),
  previewDbrFeederChain: vi.fn(),
  previewDbrProcessingChain: vi.fn(),
  previewDbrProcessingOrder: vi.fn(),
  previewDbrFeederPositions: vi.fn(),
  previewDbrFeederSignals: vi.fn(),
  rebuildDbrFeederPositions: vi.fn(),
  refreshDbrFeederChain: vi.fn(),
  refreshDbrFeederSignals: vi.fn(),
}))

const position: DbrFeederPosition = {
  id: 1,
  item_id: 100,
  item_code: 'PUMP-01',
  item_name: 'Насос ГА-1',
  warehouse_ref1c: 'MAIN',
  supply_type: 'purchase',
  mode: 'shelf',
  adu: 2,
  commonality: 1,
  red_qty: 4,
  yellow_qty: 6,
  green_qty: 8,
  target_qty: 18,
  data_quality: [],
  source_schedule_id: 7,
  is_active: true,
  is_stale: false,
  live_nfp: {
    stock_qty: 10,
    open_supply_qty: 3,
    qualified_demand_qty: 5,
    nfp: 8,
    zone: 'green',
    penetration: 0.2,
    is_complete: true,
    missing_reasons: [],
    data_quality: [],
    formula: '10 + 3 - 5',
    timestamps: { stock_as_of: '2026-07-20T08:00:00Z' },
  },
}

const purchaseSignal: DbrFeederSignal = {
  id: 201,
  dedup_key: 'purchase-201',
  signal_type: 'Пополнение',
  position_id: 1,
  item_id: 100,
  item_code: 'PUMP-01',
  item_name: 'Насос ГА-1',
  warehouse_ref1c: 'MAIN',
  status: 'Open',
  suggested_qty: 5,
  priority: 1.25,
  zone: 'red',
  kit_force: false,
  kit_shortage_qty: 0,
  can_launch: false,
  deficit_lines: [],
}

const productionSignal: DbrFeederSignal = {
  ...purchaseSignal,
  id: 202,
  dedup_key: 'production-202',
  signal_type: 'Под график',
  item_id: 101,
  item_code: 'GEAR-01',
  item_name: 'Редуктор',
  suggested_qty: 2,
  need_date: '2026-07-22',
  required_date: '2026-07-24',
  can_launch: true,
}

const deficits: DbrFeederDeficitsResult = {
  deficits: [],
  kpis: { deficit_materials: 0, queue_open: 2, stock_source: 'selected - ignored' },
}

const processingBoard: DbrProcessingBoard = {
  roundtrip_limit_days: 14,
  positions: [],
  positions_total: 0,
  overdue_positions: 0,
  generated_at: '2026-07-20T08:00:00Z',
}

const cockpit: DbrFeederCockpit = {
  meta: {
    snapshot_id: 17, ledger_generation: 42, cutoff: '2026-07-23T12:30:00Z',
    truth_status: 'accepted', chain_enabled: false, unavailable_sections: [],
  },
  positions: [position],
  signals: [purchaseSignal, productionSignal],
  deficits,
  processing_board: processingBoard,
}

const productionPreview: DbrSignalLaunchResult = {
  ok: true,
  dry_run: true,
  kind: 'feeder_signal',
  signal_id: productionSignal.id,
  entity: 'Document_ЗаказНаПроизводство2_5',
  number: 'PREVIEW-202',
  created: false,
}

const productionResult: DbrSignalLaunchResult = {
  ...productionPreview,
  dry_run: false,
  number: 'ERP-202',
  created: true,
  one_c_order_ref: 'order-ref-202',
}

const purchasePreview: DbrPurchaseLaunchResult = {
  ok: true,
  dry_run: true,
  kind: 'feeder_purchase',
  entity: 'Document_ЗаказПоставщику',
  orders_planned: 1,
  signals_total: 1,
  unresolved: [],
  already_exported: [],
  orders_created: 0,
  orders: [{
    supplier_ref1c: 'SUPPLIER-1',
    number: 'PREVIEW-PO',
    lines: [{
      item_id: 100,
      item_ref1c: 'ITEM-100',
      item_name: 'Насос ГА-1',
      qty: 5,
      source_ids: [purchaseSignal.id],
    }],
  }],
}

const purchaseResult: DbrPurchaseLaunchResult = {
  ...purchasePreview,
  dry_run: false,
  orders_created: 1,
  orders: [{ ...purchasePreview.orders[0], number: 'PO-001', status: 'created' }],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DbrFeederPage />
    </MemoryRouter>,
  )
}

describe('DbrFeederPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getDbrFeederCockpit).mockResolvedValue(cockpit)
    vi.mocked(listDbrFeederPositions).mockResolvedValue([position])
    vi.mocked(listDbrFeederSignals).mockResolvedValue([purchaseSignal, productionSignal])
    vi.mocked(getDbrFeederDeficits).mockResolvedValue(deficits)
    vi.mocked(getDbrProcessingBoard).mockResolvedValue(processingBoard)
    vi.mocked(getDbrSettings).mockResolvedValue({ feeder_chain_enabled: false } as Awaited<ReturnType<typeof getDbrSettings>>)
    vi.mocked(getDbrFeederSignal).mockImplementation(async (id) => (
      id === productionSignal.id ? productionSignal : purchaseSignal
    ))
    vi.mocked(previewDbrFeederPositions).mockResolvedValue({ schedule_id: 7, positions: [position], warnings: [] })
    vi.mocked(rebuildDbrFeederPositions).mockResolvedValue({ schedule_id: 7, positions: [position], warnings: [] })
    vi.mocked(previewDbrFeederSignals).mockResolvedValue({ schedule_id: 7, positions: 1, actionable: 2, rows: [] })
    vi.mocked(refreshDbrFeederSignals).mockResolvedValue({ schedule_id: 7, positions: 1, actionable: 2, rows: [] })
    vi.mocked(previewDbrFeederChain).mockResolvedValue({
      enabled: false,
      open_signals: 0,
      level1_children: 0,
      distinct_items: 0,
      top_items: [],
    })
    vi.mocked(refreshDbrFeederChain).mockResolvedValue({
      created: 0,
      updated: 0,
      reopened: 0,
      revoked: 0,
      no_warehouse: 0,
      passes: 0,
      disabled: true,
    })
    vi.mocked(launchDbrSignal)
      .mockResolvedValueOnce(productionPreview)
      .mockResolvedValueOnce(productionResult)
    vi.mocked(launchDbrPurchase)
      .mockResolvedValueOnce(purchasePreview)
      .mockResolvedValueOnce(purchaseResult)
  })

  it('boots one saved cockpit envelope, shows its Ledger identity and filters locally', async () => {
    const user = userEvent.setup()
    renderPage()

    const positionsTable = document.querySelector('.dbrFeederTable')
    expect(positionsTable).not.toBeNull()
    expect((await within(positionsTable as HTMLElement).findAllByText('PUMP-01'))[0]).toBeVisible()
    expect(screen.getByText('Дефицитных позиций: 0; открытых сигналов: 2')).toBeVisible()
    expect(screen.getByText('Позиций: 0; просрочен кругорейс (>14 дн): 0')).toBeVisible()
    expect(screen.getByText(/Сохранённый снимок #17.*поколение Ledger #42.*cutoff/i)).toBeVisible()
    expect(getDbrFeederCockpit).toHaveBeenCalledOnce()
    expect(getDbrFeederDeficits).not.toHaveBeenCalled()
    expect(getDbrProcessingBoard).not.toHaveBeenCalled()
    expect(getDbrSettings).not.toHaveBeenCalled()
    expect(listDbrFeederPositions).not.toHaveBeenCalled()
    expect(listDbrFeederSignals).not.toHaveBeenCalled()

    // Mount is a snapshot-read boundary. Calculations, projection refreshes
    // and 1C materialization must remain explicit user actions.
    expect(previewDbrFeederPositions).not.toHaveBeenCalled()
    expect(rebuildDbrFeederPositions).not.toHaveBeenCalled()
    expect(previewDbrFeederSignals).not.toHaveBeenCalled()
    expect(refreshDbrFeederSignals).not.toHaveBeenCalled()
    expect(previewDbrFeederChain).not.toHaveBeenCalled()
    expect(refreshDbrFeederChain).not.toHaveBeenCalled()
    expect(previewDbrProcessingChain).not.toHaveBeenCalled()
    expect(previewDbrProcessingOrder).not.toHaveBeenCalled()
    expect(getDbrProcessingTripManifest).not.toHaveBeenCalled()
    expect(getDbrProcessingTripManifestPrint).not.toHaveBeenCalled()
    expect(launchDbrSignal).not.toHaveBeenCalled()
    expect(launchDbrPurchase).not.toHaveBeenCalled()

    const positionBar = document.querySelector('.dbrFeederBar:not(.dbrSignalFilters)') as HTMLElement
    await user.type(within(positionBar).getByPlaceholderText('Код или наименование'), 'насос')
    await user.selectOptions(within(positionBar).getByRole('combobox', { name: 'Зона NFP' }), 'red')
    await user.selectOptions(within(positionBar).getByRole('combobox', { name: 'Режим позиции' }), 'shelf')
    await user.selectOptions(within(positionBar).getByRole('combobox', { name: 'Тип снабжения' }), 'purchase')
    await user.click(within(positionBar).getByRole('button', { name: 'Применить' }))
    expect(getDbrFeederCockpit).toHaveBeenCalledOnce()

    const signalBar = document.querySelector('.dbrSignalFilters') as HTMLElement
    await user.type(within(signalBar).getByPlaceholderText('Сигнал: код или наименование'), 'редуктор')
    await user.selectOptions(within(signalBar).getByRole('combobox', { name: 'Статус сигнала' }), 'Diagnostic')
    await user.selectOptions(within(signalBar).getByRole('combobox', { name: 'Зона сигнала' }), 'yellow')
    await user.selectOptions(within(signalBar).getByRole('combobox', { name: 'Тип сигнала' }), 'Под график')
    await user.click(within(signalBar).getByRole('button', { name: 'Применить' }))
    expect(getDbrFeederCockpit).toHaveBeenCalledOnce()
  })

  it('filters saved signals locally without another read request', async () => {
    const user = userEvent.setup()
    renderPage()

    const signalBar = document.querySelector('.dbrSignalFilters') as HTMLElement
    await user.selectOptions(
      within(signalBar).getByRole('combobox', { name: 'Статус сигнала' }),
      'Open',
    )
    await user.type(within(signalBar).getByPlaceholderText('Сигнал: код или наименование'), 'редуктор{Enter}')

    const signalTable = document.querySelector('.dbrSignalTable') as HTMLElement
    expect(await within(signalTable).findByText('GEAR-01')).toBeVisible()
    expect(within(signalTable).queryByText('PUMP-01')).not.toBeInTheDocument()
    expect(getDbrFeederCockpit).toHaveBeenCalledOnce()
  })

  it('requires a dry-run preview before confirming a production launch', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Запустить…' }))
    expect(launchDbrSignal).toHaveBeenNthCalledWith(1, productionSignal.id, true)

    const dialog = await screen.findByRole('dialog', { name: 'Запуск сигнала — GEAR-01' })
    expect(within(dialog).getByText(/Будет создан документ в живой 1С/)).toBeVisible()
    expect(within(dialog).getByText(/PREVIEW-202/)).toBeVisible()

    await user.click(within(dialog).getByRole('button', { name: 'Провести в 1С' }))
    await waitFor(() => expect(launchDbrSignal).toHaveBeenNthCalledWith(2, productionSignal.id, false))
    expect(await within(dialog).findByText(/Заказ создан в 1С/)).toBeVisible()
    expect(dialog).toHaveTextContent('ERP-202')
    expect(getDbrFeederCockpit).toHaveBeenCalledTimes(2)
  })

  it('turns a material conflict into a blocked launch without a confirm action', async () => {
    const user = userEvent.setup()
    vi.mocked(launchDbrSignal).mockReset().mockRejectedValueOnce(new ApiError(
      'Недостаточно материала',
      409,
      {
        deficit_lines: [{
          item: 'BEARING-01',
          item_name: 'Подшипник',
          article: 'ПД-01',
          need: 4,
          have: 1,
          gross: 1,
          kind: 'buy',
          level: '1',
          cls: 'no',
          buffered: false,
        }],
      },
    ))
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Запустить…' }))

    const dialog = await screen.findByRole('dialog', { name: 'Запуск сигнала — GEAR-01' })
    expect(within(dialog).getByText('Недостаточно материала')).toBeVisible()
    expect(within(dialog).getByText('Подшипник')).toBeVisible()
    expect(within(dialog).queryByRole('button', { name: 'Провести в 1С' })).not.toBeInTheDocument()
    expect(launchDbrSignal).toHaveBeenCalledOnce()
    expect(launchDbrSignal).toHaveBeenCalledWith(productionSignal.id, true)
  })

  it('previews and confirms a supplier order for the selected open replenishment signal', async () => {
    const user = userEvent.setup()
    renderPage()

    const selectSignal = await screen.findByRole('checkbox', { name: 'Выбрать сигнал PUMP-01 для заказа поставщику' })
    await user.click(selectSignal)
    await user.click(screen.getByRole('button', { name: 'Заказать поставщику… (1)' }))

    expect(launchDbrPurchase).toHaveBeenNthCalledWith(1, [purchaseSignal.id], true)
    const dialog = await screen.findByRole('dialog', { name: 'Заказ поставщику по сигналам питателя' })
    expect(within(dialog).getByText('Выбрано сигналов: 1.')).toBeVisible()
    expect(within(dialog).getByText(/Поставщик SUPPLIER-1/)).toBeVisible()

    await user.click(within(dialog).getByRole('button', { name: 'Провести в 1С' }))
    await waitFor(() => expect(launchDbrPurchase).toHaveBeenNthCalledWith(2, [purchaseSignal.id], false))
    expect(await within(dialog).findByText(/Заказов: 1 · создано: 1/)).toBeVisible()
    expect(selectSignal).not.toBeChecked()
    expect(getDbrFeederCockpit).toHaveBeenCalledTimes(2)
  })

  it('keeps a positions preview after a failed rebuild and lets the user retry it', async () => {
    const user = userEvent.setup()
    vi.mocked(previewDbrFeederPositions).mockResolvedValue({
      schedule_id: 77,
      positions: [position],
      warnings: ['У позиции отсутствует норматив'],
    })
    vi.mocked(rebuildDbrFeederPositions)
      .mockRejectedValueOnce(new Error('График изменился'))
      .mockResolvedValueOnce({
        schedule_id: 77,
        positions: [position],
        warnings: [],
        created: 1,
        updated: 2,
        deactivated: 3,
      })
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Предпросмотр пересчёта' }))
    const preview = await screen.findByRole('region', { name: 'Предпросмотр пересчёта' })
    expect(within(preview).getByText('График №77: 1 позиций')).toBeVisible()
    await user.click(within(preview).getByText('Показать предупреждения'))
    expect(within(preview).getByText('У позиции отсутствует норматив')).toBeVisible()

    const rebuild = within(preview).getByRole('button', { name: 'Перестроить по графику №77' })
    await user.click(rebuild)
    expect(await screen.findByText('График изменился')).toBeVisible()
    expect(preview).toBeVisible()
    expect(rebuildDbrFeederPositions).toHaveBeenNthCalledWith(1, 77)

    await user.click(rebuild)
    expect(await screen.findByText('Позиции обновлены по графику №77: создано 1, обновлено 2, отключено 3')).toBeVisible()
    expect(rebuildDbrFeederPositions).toHaveBeenNthCalledWith(2, 77)
    expect(screen.queryByRole('region', { name: 'Предпросмотр пересчёта' })).not.toBeInTheDocument()
    expect(getDbrFeederCockpit).toHaveBeenCalledTimes(2)
  })

  it('previews advisory changes before refreshing signals for that schedule', async () => {
    const user = userEvent.setup()
    vi.mocked(previewDbrFeederSignals).mockResolvedValue({
      schedule_id: 88,
      positions: 3,
      actionable: 2,
      diagnostic: 1,
      rows: [
        {
          signal_type: 'Пополнение',
          position_id: 1,
          item_id: 100,
          item_code: 'PUMP-01',
          warehouse_ref1c: 'MAIN',
          zone: 'red',
          priority: 2,
          nfp: 1,
          target_qty: 8,
          kit_force: false,
          kit_shortage_qty: 0,
          suggested_qty: 7,
          is_complete: true,
          action: 'open',
        },
        {
          signal_type: 'Под график',
          position_id: 2,
          item_id: 101,
          item_code: 'GEAR-01',
          warehouse_ref1c: 'MAIN',
          zone: 'yellow',
          priority: 1,
          nfp: 2,
          target_qty: 5,
          kit_force: false,
          kit_shortage_qty: 0,
          suggested_qty: 3,
          is_complete: true,
          action: 'update',
        },
      ],
    })
    vi.mocked(refreshDbrFeederSignals).mockResolvedValue({
      schedule_id: 88,
      positions: 3,
      actionable: 2,
      rows: [],
      created: 1,
      updated: 1,
      reopened: 0,
      cancelled: 0,
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Предпросмотр сигналов' }))
    const previewTitle = await screen.findByText('График №88: 2 актуальных сигналов')
    const preview = previewTitle.closest('.dbrSignalPreview') as HTMLElement
    expect(preview).not.toBeNull()
    expect(within(preview).getByText('График №88: 2 актуальных сигналов')).toBeVisible()
    expect(within(preview).getByText('Пополнение: 1; под график: 1')).toBeVisible()

    await user.click(within(preview).getByRole('button', { name: 'Обновить по графику №88' }))
    expect(await screen.findByText(/Advisory-очередь обновлена по графику №88/)).toBeVisible()
    expect(refreshDbrFeederSignals).toHaveBeenCalledWith(88)
    expect(getDbrFeederCockpit).toHaveBeenCalledTimes(2)
  })

  it('opens signal details and drills from a deficit into the blocked queue', async () => {
    const user = userEvent.setup()
    const deficitLine: NonNullable<DbrFeederSignal['deficit_lines']>[number] = {
      item: 'BEARING-01',
      item_name: 'Подшипник',
      article: 'ПД-01',
      need: 4,
      have: 1,
      gross: 1,
      kind: 'buy',
      level: '1',
      cls: 'no',
      buffered: false,
    }
    const blockedSignal: DbrFeederSignal = {
      ...purchaseSignal,
      deficit_lines: [deficitLine],
      material_status: 'Дефицит',
      kit_cls: 'no',
    }
    const blockedDeficits: DbrFeederDeficitsResult = {
      deficits: [{
        item: 'BEARING-01',
        item_name: 'Подшипник',
        article: 'ПД-01',
        source: 'buy',
        short_qty: 3,
        need_sum: 4,
        gross: 1,
        blocks_signals: 1,
        nearest_due: '2026-07-22',
      }],
      kpis: { deficit_materials: 1, queue_open: 2, stock_source: 'selected - ignored' },
    }
    vi.mocked(getDbrFeederCockpit).mockResolvedValue({ ...cockpit, signals: [blockedSignal, productionSignal], deficits: blockedDeficits })
    renderPage()

    const signalTable = document.querySelector('.dbrSignalTable') as HTMLElement
    const productionRow = await within(signalTable).findByRole('row', { name: /GEAR-01/ })
    await user.click(productionRow)
    const detail = await screen.findByRole('complementary', { name: 'Карточка сигнала' })
    expect(within(detail).getByText(`Сигнал #${productionSignal.id}`)).toBeVisible()
    expect(getDbrFeederSignal).not.toHaveBeenCalled()

    const deficitTable = document.querySelector('.dbrDeficitTable') as HTMLElement
    await user.click(await within(deficitTable).findByRole('row', { name: /BEARING-01/ }))
    expect(await within(signalTable).findByText('PUMP-01')).toBeVisible()
    expect(within(signalTable).queryByText('GEAR-01')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Дефицит: BEARING-01 ✕' })).toBeVisible()
    expect(screen.queryByRole('complementary', { name: 'Карточка сигнала' })).not.toBeInTheDocument()
  })

  it('gates chain controls by settings and refreshes from its read-only preview', async () => {
    const user = userEvent.setup()
    vi.mocked(getDbrFeederCockpit).mockResolvedValue({ ...cockpit, meta: { ...cockpit.meta, chain_enabled: true } })
    vi.mocked(previewDbrFeederChain).mockResolvedValue({
      enabled: true,
      open_signals: 4,
      level1_children: 2,
      distinct_items: 1,
      top_items: [{ item: 'BLANK-01', parents: 2, qty_sum: 6 }],
    })
    vi.mocked(refreshDbrFeederChain).mockResolvedValue({
      created: 2,
      updated: 1,
      reopened: 0,
      revoked: 0,
      no_warehouse: 0,
      passes: 1,
    })
    renderPage()

    const previewButton = await screen.findByRole('button', { name: 'Цепочка: предпросмотр' })
    await user.click(previewButton)
    const dialog = await screen.findByRole('dialog', { name: 'Предпросмотр цепочки' })
    expect(within(dialog).getByText('BLANK-01')).toBeVisible()
    expect(within(dialog).getByText(/Предпросмотр не создаёт сигналы/)).toBeVisible()

    await user.click(within(dialog).getByRole('button', { name: 'Цепочка: обновить' }))
    expect(await screen.findByText(/Цепочка обновлена: создано 2, обновлено 1/)).toBeVisible()
    expect(refreshDbrFeederChain).toHaveBeenCalledOnce()
    expect(getDbrFeederCockpit).toHaveBeenCalledTimes(2)
  })

  it('separates exact contractor stock and keeps processing actions read-only', async () => {
    const user = userEvent.setup()
    vi.mocked(getDbrFeederCockpit).mockResolvedValue({ ...cockpit, processing_board: {
      roundtrip_limit_days: 14,
      positions_total: 1,
      overdue_positions: 0,
      generated_at: '2026-07-20T08:00:00Z',
      processing_stock: { status: 'ok', rows_stored: 2, total_qty: 7, last_success_at: '2026-07-20T07:00:00Z' },
      contractors: [{
        supplier_id: 3,
        supplier_ref1c: 'SUP-3',
        supplier_name: 'Гальваника',
        roundtrip_kpi: { semantics: 'proxy', eligible_rows: 1, completed_rows: 1, completed_orders: 1, completed_qty: 5, weighted_avg_days: 6, max_days: 6, within_roundtrip_rows: 1, within_roundtrip_qty: 5, invalid_date_rows: 0 },
      }],
      positions: [{
        position_id: 9, item_id: purchaseSignal.item_id, item_code: 'PUMP-01', item_article: '', item_name: 'Насос',
        adu: 1, rt_days: 14, trip_interval_days: 7, red_qty: 2, yellow_qty: 2, target_qty: 6,
        nfp: 4, zone: 'yellow', stock_qty: 1, open_supply_qty: 11, at_contractor_qty: 7, chain_supply_qty: 2,
        missing_reasons: [], open_orders: [], has_overdue: false,
        roundtrip_kpi: { semantics: 'proxy', eligible_rows: 1, completed_rows: 1, completed_orders: 1, completed_qty: 5, weighted_avg_days: 6, max_days: 6, within_roundtrip_rows: 1, within_roundtrip_qty: 5, invalid_date_rows: 0 },
      }],
    } })
    vi.mocked(previewDbrProcessingChain).mockResolvedValue({
      read_only: true, processing_open_signals: 1, netted_signals: 1, desired_children: 1,
      distinct_components: 1, parents_with_children: 1, unresolved_count: 0, unresolved: [],
      children: [{ parent_signal_id: purchaseSignal.id, parent_item: 'PUMP-01', component_item: 'BARE-01', suggested_qty: 3, shortage_qty: 3, warehouse_ref1c: 'WH', unresolved_reasons: [] }],
    })
    vi.mocked(previewDbrProcessingOrder).mockResolvedValue({
      dry_run: true, write_capable: false, live_contract_confirmed: false, gate: 'blocked_until_1c_contract_confirmation',
      entity: 'Document_ЗаказПоставщику', signal_id: purchaseSignal.id, payload: { ВидОперации: 'ЗаказНаПереработку' },
    })
    renderPage()

    expect(await screen.findByText(/Точный остаток у переработчика: синхронизирован/)).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Открытая труба' })).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Точно у переработчика' })).toBeVisible()
    expect(screen.getByText(/Кругорейс по подрядчикам/)).toBeVisible()
    expect(screen.getByText(/запись заказа переработчику в 1С отключена до demo-smoke/i)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Цепочка: проверить' }))
    expect(await screen.findByRole('dialog', { name: 'Проверка цепочки переработки' })).toHaveTextContent('BARE-01')
    await user.click(screen.getByRole('button', { name: 'Закрыть' }))

    await user.selectOptions(screen.getByRole('combobox', { name: /Предпросмотр заказа/ }), String(purchaseSignal.id))
    expect(await screen.findByRole('dialog', { name: 'Предпросмотр заказа переработчику' })).toHaveTextContent('ЗаказНаПереработку')
    expect(screen.getByText(/Запись в 1С отключена до demo-smoke/)).toBeVisible()
  })

  it('filters positions locally without loading a new generation', async () => {
    const user = userEvent.setup()
    renderPage()

    const positionsTable = document.querySelector('.dbrFeederTable') as HTMLElement
    expect((await within(positionsTable).findAllByText('PUMP-01'))[0]).toBeVisible()
    const search = screen.getByPlaceholderText('Код или наименование')
    await user.type(search, 'нет{Enter}')
    expect(within(positionsTable).queryByText('PUMP-01')).not.toBeInTheDocument()
    expect(getDbrFeederCockpit).toHaveBeenCalledOnce()
  })

  it('opens signal details from the same cockpit without an extra GET', async () => {
    renderPage()

    const signalTable = document.querySelector('.dbrSignalTable') as HTMLElement
    fireEvent.click(await within(signalTable).findByRole('row', { name: /GEAR-01/ }))
    expect(await screen.findByText(`Сигнал #${productionSignal.id}`)).toBeVisible()
    expect(getDbrFeederSignal).not.toHaveBeenCalled()
    expect(getDbrFeederCockpit).toHaveBeenCalledOnce()
  })

  it('renders structured 503 and unavailable sections without zero facts', async () => {
    vi.mocked(getDbrFeederCockpit).mockRejectedValueOnce(new ApiError('No cockpit', 503, {
      code: 'dbr_cockpit_snapshot_unavailable', reason: 'No DBR feeder cockpit snapshot for current accepted Ledger',
    }))
    renderPage()
    expect(await screen.findByText(/dbr_cockpit_snapshot_unavailable/)).toBeVisible()
  })

  it('keeps a read-only Ledger snapshot inspectable while exposing no legacy or live actions', async () => {
    vi.mocked(getDbrFeederCockpit).mockResolvedValue({
      ...cockpit,
      meta: { ...cockpit.meta, read_only: true, chain_enabled: true },
    })
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('note')).toHaveTextContent('Режим только чтение')
    expect(screen.getByRole('note')).toHaveTextContent('снимок Item Ledger')
    expect(screen.queryByRole('button', { name: 'Предпросмотр пересчёта' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Предпросмотр сигналов' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Заказать поставщику/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Запустить…' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Цепочка:/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Рейс:/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /закупочн|поставщик/i })).not.toBeInTheDocument()

    const signalTable = document.querySelector('.dbrSignalTable') as HTMLElement
    await user.click(await within(signalTable).findByRole('row', { name: /GEAR-01/ }))
    expect(await screen.findByRole('complementary', { name: 'Карточка сигнала' })).toHaveTextContent('Действия отключены')
    expect(previewDbrFeederPositions).not.toHaveBeenCalled()
    expect(previewDbrFeederSignals).not.toHaveBeenCalled()
    expect(refreshDbrFeederSignals).not.toHaveBeenCalled()
    expect(refreshDbrFeederChain).not.toHaveBeenCalled()
    expect(previewDbrProcessingChain).not.toHaveBeenCalled()
    expect(previewDbrProcessingOrder).not.toHaveBeenCalled()
    expect(getDbrProcessingTripManifest).not.toHaveBeenCalled()
    expect(launchDbrSignal).not.toHaveBeenCalled()
    expect(launchDbrPurchase).not.toHaveBeenCalled()
  })
})
